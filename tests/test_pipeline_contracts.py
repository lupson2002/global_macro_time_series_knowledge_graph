import json
import tempfile
import unittest
from pathlib import Path

from main import check_processed, is_macro_relevant, load_channels
from src.exporter import SQLiteExporter


class RelevanceGateTests(unittest.TestCase):
    def test_neutral_empty_view_is_skipped(self):
        self.assertFalse(is_macro_relevant({
            "graph_nodes": {"specific_tickers": []},
            "quant_signals": {"bull_bear_score": 5, "conviction_score": 5},
        }))

    def test_each_supported_signal_is_relevant(self):
        cases = [
            {"graph_nodes": {"specific_tickers": ["[[TLT]]"]}},
            {"quant_signals": {"duration_call": "Long"}},
            {"quant_signals": {"macro_factor": "Inflation"}},
            {"quant_signals": {"view_time_horizon": "Months"}},
            {"quant_signals": {"bull_bear_score": 4}},
            {"quant_signals": {"conviction_score": 6}},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertTrue(is_macro_relevant(data))

    def test_sector_tilt_alone_is_not_relevant(self):
        self.assertFalse(is_macro_relevant({
            "graph_nodes": {"specific_tickers": []},
            "quant_signals": {
                "bull_bear_score": 5,
                "conviction_score": 5,
                "sector_tilt": "[[Food Technology]]",
            },
        }))


class PipelineIdempotencyTests(unittest.TestCase):
    def test_skipped_video_is_considered_processed(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "macro.db"
            exporter = SQLiteExporter(str(db))
            exporter.mark_skipped("abcdefghijk", "not_macro_relevant")
            self.assertTrue(check_processed(str(db), "abcdefghijk"))
            self.assertFalse(check_processed(str(db), "abcdefghijk", include_skipped=False))

    def test_missing_database_is_not_processed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(check_processed(str(Path(d) / "missing.db"), "abcdefghijk"))


class ChannelConfigurationTests(unittest.TestCase):
    def test_disabled_tier_is_excluded_by_default(self):
        config = {
            "tiers": {
                "enabled": {
                    "_enabled": True,
                    "channels": [{"name": "A", "channel_id": "UC-A"}],
                },
                "disabled": {
                    "_enabled": False,
                    "channels": [{"name": "B", "channel_id": "UC-B"}],
                },
            }
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "channels.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(load_channels(path), {"A": "UC-A"})
            self.assertEqual(
                load_channels(path, include_disabled=True),
                {"A": "UC-A", "B": "UC-B"},
            )
