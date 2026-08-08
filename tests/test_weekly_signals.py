import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.weekly_signals import build_signal_snapshot, changes_fingerprint, material_changes


class WeeklySignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "macro.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE reports (
                    video_id TEXT PRIMARY KEY, source_channel TEXT,
                    speaker_name TEXT, broadcast_date TEXT
                );
                CREATE TABLE quant_signals (
                    video_id TEXT PRIMARY KEY, bull_bear_score REAL,
                    conviction_score REAL, contrarian_flag INTEGER
                );
                CREATE TABLE nodes (video_id TEXT, node_type TEXT, node_value TEXT);
            """)

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, video, channel, speaker, day, score, asset="Equities", theme="AI"):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO reports VALUES (?,?,?,?)", (video, channel, speaker, day,))
            conn.execute("INSERT INTO quant_signals VALUES (?,?,?,?)", (video, score, 8, score <= 4))
            conn.execute("INSERT INTO nodes VALUES (?,?,?)", (video, "asset_class", asset))
            conn.execute("INSERT INTO nodes VALUES (?,?,?)", (video, "macro_theme", theme))

    def test_snapshot_balances_channels_and_compares_adjacent_weeks(self):
        # Five prolific bullish videos in A count as one channel view; bearish B is equal weight.
        for idx in range(5):
            self._add(f"a{idx}", "A", "Same", "2026-08-08", 9)
        self._add("b", "B", "Other", "2026-08-08", 3)
        self._add("c", "C", "Third", "2026-08-08", 6)
        self._add("old-a", "A", "Same", "2026-08-01", 5)
        self._add("old-b", "B", "Other", "2026-08-01", 5)
        self._add("old-c", "C", "Third", "2026-08-01", 5)

        snapshot = build_signal_snapshot(self.db, today=date(2026, 8, 8))

        self.assertEqual(snapshot["overall"]["channels"], 3)
        self.assertEqual(snapshot["overall"]["stance"], 56)
        self.assertEqual(snapshot["overall"]["previous_stance"], 44)
        self.assertEqual(snapshot["overall"]["delta"], 12)
        self.assertEqual(snapshot["assets"][0]["speakers"], 3)

    def test_material_change_thresholds_and_fingerprint_are_deterministic(self):
        baseline = {
            "overall": {"stance": 50, "tail_risk_ratio": 0.0},
            "assets": [{"asset": "Equities", "stance": 50}],
        }
        current = {
            "overall": {"stance": 61, "tail_risk_ratio": 0.11},
            "assets": [{"asset": "Equities", "stance": 63, "speakers": 4, "channels": 2}],
            "narrative_velocity": [{"node": "Inflation", "count_7d": 7, "velocity": 2.2, "new": False}],
        }
        changes = material_changes(current, baseline)
        self.assertEqual([item["kind"] for item in changes], [
            "market_stance", "tail_risk", "asset_stance", "narrative_acceleration",
        ])
        self.assertEqual(changes_fingerprint(changes), changes_fingerprint(list(changes)))


if __name__ == "__main__":
    unittest.main()
