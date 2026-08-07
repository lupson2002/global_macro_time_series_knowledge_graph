import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.exporter import ObsidianMDExporter, SQLiteExporter, _load_db_report_as_schema

from helpers import macro_view


class SQLiteExporterTests(unittest.TestCase):
    def test_initialization_migrates_legacy_tables(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "legacy.db"
            with sqlite3.connect(db) as con:
                con.execute("""CREATE TABLE reports (
                    video_id TEXT PRIMARY KEY, speaker_name TEXT, speaker_role TEXT,
                    source_channel TEXT, broadcast_date TEXT, time_box TEXT,
                    core_thesis TEXT, verbatim_quote TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                con.execute("""CREATE TABLE quant_signals (
                    video_id TEXT PRIMARY KEY, bull_bear_score INTEGER,
                    conviction_score INTEGER, contrarian_flag INTEGER)""")
                con.execute("""CREATE TABLE nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT,
                    node_type TEXT, node_value TEXT)""")
            SQLiteExporter(str(db))
            with sqlite3.connect(db) as con:
                report_cols = {r[1] for r in con.execute("PRAGMA table_info(reports)")}
                signal_cols = {r[1] for r in con.execute("PRAGMA table_info(quant_signals)")}
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("tactical_stance", report_cols)
            self.assertIn("view_time_horizon", signal_cols)
            self.assertIn("skipped_videos", tables)
            self.assertIn("daily_sentiment", tables)

    def test_upsert_preserves_created_at_and_replaces_children(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "macro.db"
            exporter = SQLiteExporter(str(db))
            first = macro_view()
            with patch("src.lancedb_store.upsert_document"):
                exporter.export_data(first)
            with sqlite3.connect(db) as con:
                con.execute(
                    "UPDATE reports SET created_at='2020-01-01 00:00:00' WHERE video_id=?",
                    ("abcdefghijk",),
                )
                con.commit()
            second = macro_view()
            second["view_details"]["core_thesis"] = "Updated thesis"
            second["graph_nodes"]["macro_themes"] = ["[[Liquidity]]"]
            with patch("src.lancedb_store.upsert_document"):
                exporter.export_data(second)
            with sqlite3.connect(db) as con:
                report = con.execute(
                    "SELECT core_thesis, created_at FROM reports WHERE video_id=?",
                    ("abcdefghijk",),
                ).fetchone()
                nodes = con.execute(
                    "SELECT node_type, node_value FROM nodes WHERE video_id=? ORDER BY id",
                    ("abcdefghijk",),
                ).fetchall()
            self.assertEqual(report, ("Updated thesis", "2020-01-01 00:00:00"))
            self.assertIn(("macro_theme", "[[Liquidity]]"), nodes)
            self.assertNotIn(("macro_theme", "[[Inflation]]"), nodes)

    def test_db_round_trip_restores_json_fields(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "macro.db"
            exporter = SQLiteExporter(str(db))
            with patch("src.lancedb_store.upsert_document"):
                exporter.export_data(macro_view())
            rows = list(_load_db_report_as_schema(str(db)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["view_details"]["conditional_catalysts"], ["CPI cools"])
            self.assertEqual(rows[0]["causal_chain"], ["CPI down", "Yields down", "Bonds up"])
            self.assertEqual(rows[0]["quant_signals"]["view_time_horizon"], "Months")


class ObsidianExporterTests(unittest.TestCase):
    def test_markdown_preserves_backlinks_and_escapes_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            data = macro_view(
                speaker_name='Analyst: "Alpha"/Beta',
                speaker_role="Lead\nStrategist",
            )
            path = ObsidianMDExporter(d).export_markdown(data)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(path.parent.name, "2026-08-07")
            self.assertNotIn("/", path.name)
            self.assertIn('speaker: "Analyst: \\"Alpha\\"/Beta"', text)
            self.assertIn('role: "Lead\\nStrategist"', text)
            self.assertIn("[[Inflation]]", text)
            self.assertIn("[[TLT]]", text)
            self.assertIn("* CPI cools", text)
            self.assertIn("Inflation reaccelerates", text)
