import json
import unittest
from unittest.mock import Mock

from src.llm_response import ExtractionResponseProcessor
from tests.helpers import macro_view


class ExtractionResponseProcessorTests(unittest.TestCase):
    def test_override_and_normalization_happen_before_validation(self):
        data = macro_view()
        data["metadata"].update(
            video_id="hallucinated",
            source_channel="Wrong",
            broadcast_date="1999-01-01",
        )
        data["graph_nodes"]["time_box"] = "2026-H2"
        validated = []
        processor = ExtractionResponseProcessor(
            validator=lambda parsed: validated.append(parsed.copy())
        )

        result = processor.process(
            json.dumps(data),
            video_id="abcdefghijk",
            source_channel="Trusted",
            upload_date="2026-08-07",
        )

        self.assertEqual(result["metadata"]["video_id"], "abcdefghijk")
        self.assertEqual(result["metadata"]["source_channel"], "Trusted")
        self.assertEqual(result["metadata"]["broadcast_date"], "2026-08-07")
        self.assertEqual(result["graph_nodes"]["time_box"], "[[2026-H2]]")
        self.assertEqual(validated[0]["metadata"], result["metadata"])

    def test_parse_recovery_is_called_once_then_returns_valid_data(self):
        recovery = Mock(return_value=json.dumps(macro_view()))
        processor = ExtractionResponseProcessor(validator=Mock())

        result = processor.process(
            "not-json",
            video_id="abcdefghijk",
            source_channel="Trusted",
            upload_date=None,
            recover=recovery,
        )

        self.assertEqual(result["metadata"]["video_id"], "abcdefghijk")
        recovery.assert_called_once_with()

    def test_second_parse_failure_stops_after_one_recovery(self):
        recovery = Mock(return_value="still-not-json")
        processor = ExtractionResponseProcessor(validator=Mock())

        with self.assertRaisesRegex(RuntimeError, "after 2 attempts"):
            processor.process(
                "not-json",
                video_id="abcdefghijk",
                source_channel="Trusted",
                upload_date=None,
                recover=recovery,
            )

        recovery.assert_called_once_with()

    def test_schema_validation_remains_soft(self):
        validator = Mock(side_effect=ValueError("incomplete"))
        processor = ExtractionResponseProcessor(validator=validator)

        result = processor.process(
            '{"metadata":{"video_id":"wrong"}}',
            video_id="abcdefghijk",
            source_channel="Trusted",
            upload_date=None,
        )

        self.assertEqual(result["metadata"]["video_id"], "abcdefghijk")
        validator.assert_called_once_with(result)
