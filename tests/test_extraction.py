import json
import unittest
from unittest.mock import patch

from src.local_llm_client import LocalLLMClient, _extract_json, post_process_json

from helpers import macro_view


class JsonExtractionTests(unittest.TestCase):
    def test_extracts_fenced_json_with_surrounding_prose(self):
        raw = 'Before\n```json\n{"text":"brace { inside }", "nested":{"ok":true}}\n```\nAfter'
        self.assertEqual(
            json.loads(_extract_json(raw)),
            {"text": "brace { inside }", "nested": {"ok": True}},
        )

    def test_rejects_empty_or_non_json_response(self):
        for raw in ("", "plain prose"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _extract_json(raw)

    def test_post_process_normalizes_graph_and_tactical_nodes(self):
        data = {
            "graph_nodes": {
                "time_box": "2026-H2",
                "macro_themes": ["Inflation"],
                "asset_classes": ["[[Bonds]]"],
                "specific_tickers": ["TLT"],
            },
            "quant_signals": {
                "sector_tilt": "Defensives",
                "duration_call": "[[Long]]",
                "macro_factor": "[[Inflation]]",
                "view_time_horizon": "[[Months]]",
            },
            "view_details": {"price_targets": [{"ticker": "TLT"}]},
        }
        out = post_process_json(data)
        self.assertEqual(out["graph_nodes"]["time_box"], "[[2026-H2]]")
        self.assertEqual(out["graph_nodes"]["macro_themes"], ["[[Inflation]]"])
        self.assertEqual(out["quant_signals"]["sector_tilt"], "[[Defensives]]")
        self.assertEqual(out["quant_signals"]["duration_call"], "Long")
        self.assertEqual(out["view_details"]["price_targets"][0]["ticker"], "[[TLT]]")


class AnalysisContractTests(unittest.TestCase):
    def test_rejects_empty_transcript_before_network_call(self):
        client = LocalLLMClient()
        with patch.object(client, "_chat") as chat:
            with self.assertRaises(ValueError):
                client.analyze_transcript("  ", "abcdefghijk")
        chat.assert_not_called()

    def test_overrides_trusted_source_metadata(self):
        data = macro_view()
        data["metadata"].update(
            video_id="wrong-video",
            source_channel="Hallucinated",
            broadcast_date="1999-01-01",
        )
        client = LocalLLMClient()
        out = client._parse_and_validate(
            json.dumps(data),
            "abcdefghijk",
            "Trusted_Channel",
            "2026-08-07",
            "transcript",
        )
        self.assertEqual(out["metadata"]["video_id"], "abcdefghijk")
        self.assertEqual(out["metadata"]["source_channel"], "Trusted_Channel")
        self.assertEqual(out["metadata"]["broadcast_date"], "2026-08-07")

    def test_parse_retry_refeeds_complete_original_transcript(self):
        transcript = "HEAD-" + ("middle " * 12_000) + "-TAIL"
        valid = json.dumps(macro_view())
        client = LocalLLMClient()
        with patch.object(client, "_chat", return_value=valid) as chat:
            out = client._parse_and_validate(
                "not-json",
                "abcdefghijk",
                "Trusted_Channel",
                "2026-08-07",
                transcript,
            )
        self.assertEqual(out["metadata"]["video_id"], "abcdefghijk")
        retry_prompt = chat.call_args.kwargs["user"]
        self.assertIn(transcript, retry_prompt)
        self.assertIn("HEAD-", retry_prompt)
        self.assertIn("middle", retry_prompt)
        self.assertIn("-TAIL", retry_prompt)

    def test_schema_validation_remains_soft(self):
        incomplete = {"metadata": {"video_id": "wrong"}}
        client = LocalLLMClient()
        out = client._parse_and_validate(
            json.dumps(incomplete), "abcdefghijk", "Trusted", None, "text"
        )
        self.assertEqual(out["metadata"]["video_id"], "abcdefghijk")
