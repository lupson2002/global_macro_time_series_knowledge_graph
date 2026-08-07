import unittest
from unittest.mock import Mock, patch

from src.local_llm_client import LocalLLMClient


class FullTranscriptTests(unittest.TestCase):
    def test_chat_delegates_retry_budget_once_to_provider_layer(self):
        client = LocalLLMClient()
        completion = Mock(return_value="ok")
        with patch("src.cloud_client.chat_completion", completion):
            self.assertEqual(client._chat("system", "user", max_retries=4), "ok")
        completion.assert_called_once()
        self.assertEqual(completion.call_args.kwargs["ollama_attempts"], 4)

    def test_analyze_transcript_passes_entire_large_input(self):
        transcript = "HEAD" + ("middle-marker " * 7_000) + "TAIL"
        captured = []

        def fake_chat(self, system, user, response_format_json=False, max_tokens=None, max_retries=3):
            captured.append(user)
            return """{
          "metadata": {
            "speaker_name": "Test", "speaker_role": "Analyst",
            "source_channel": "Test_Channel", "broadcast_date": "2026-08-07",
            "video_id": "abcdefghijk"
          },
          "graph_nodes": {
            "time_box": "2026-H2", "macro_themes": [],
            "asset_classes": [], "specific_tickers": []
          },
          "quant_signals": {
            "bull_bear_score": 5, "conviction_score": 5,
            "contrarian_flag": false
          },
          "view_details": {
            "core_thesis": "Test", "conditional_catalysts": [],
            "invalidation_risks": [], "verbatim_quote": "Test"
          }
            }"""

        with patch.object(LocalLLMClient, "_chat", fake_chat):
            client = LocalLLMClient()
            client.analyze_transcript(transcript, "abcdefghijk", "Test_Channel")

        self.assertGreater(len(transcript), 60_000)
        self.assertIn(transcript, captured[0])
        self.assertIn("HEAD", captured[0])
        self.assertIn("middle-marker", captured[0])
        self.assertIn("TAIL", captured[0])
        self.assertNotIn("[중략:", captured[0])


if __name__ == "__main__":
    unittest.main()
