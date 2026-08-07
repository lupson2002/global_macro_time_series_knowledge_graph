import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.run_events import JsonlEventSink, ReportRunJournal, RunJournal, SafeEventEmitter


class RunEventTests(unittest.TestCase):
    def test_jsonl_sink_appends_stable_unicode_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "events.jsonl"
            emitter = SafeEventEmitter(
                JsonlEventSink(path),
                warn=self.fail,
                clock=lambda: datetime(2026, 8, 7, 3, 4, 5, tzinfo=timezone.utc),
            )
            emitter.emit(
                "video.finished", "success", "run-1",
                video_id="abcdefghijk", source="한국경제",
                stage="storage", details={"transcript_chars": 123},
            )
            emitter.emit("run.finished", "success", "run-1")

            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([record["event"] for record in records], ["video.finished", "run.finished"])
        self.assertEqual(records[0]["source"], "한국경제")
        self.assertEqual(records[0]["timestamp"], "2026-08-07T03:04:05+00:00")
        self.assertEqual(records[0]["schema_version"], 1)

    def test_no_sink_is_a_no_op(self):
        warn = Mock()
        SafeEventEmitter(None, warn=warn).emit("run.started", "running", "run-1")
        warn.assert_not_called()

    def test_report_lifecycle_uses_the_same_serializable_schema(self):
        sink = Mock()
        emitter = SafeEventEmitter(sink, warn=self.fail)

        emitter.emit(
            "report.finished", "success", "run-1",
            report="daily_macro", stage="delivery",
        )

        record = sink.emit.call_args.args[0].as_dict()
        self.assertEqual(record["report"], "daily_macro")
        self.assertEqual(record["stage"], "delivery")

    def test_sink_failure_warns_once_and_does_not_escape(self):
        sink = Mock()
        sink.emit.side_effect = OSError("disk full")
        warn = Mock()
        emitter = SafeEventEmitter(sink, warn=warn)

        emitter.emit("run.started", "running", "run-1")
        emitter.emit("run.finished", "success", "run-1")

        sink.emit.assert_called_once()
        warn.assert_called_once()
        self.assertIn("disk full", warn.call_args.args[0])

    def test_report_run_journal_pairs_run_and_report_lifecycle(self):
        sink = Mock()
        journal = ReportRunJournal(RunJournal(SafeEventEmitter(sink, warn=self.fail), "run-1"), "cio")

        journal.started()
        journal.finished(success=False, stage="generation", error=ValueError("private"))

        records = [item.args[0].as_dict() for item in sink.emit.call_args_list]
        self.assertEqual(
            [record["event"] for record in records],
            ["run.started", "report.started", "report.finished", "run.finished"],
        )
        self.assertEqual(records[2]["details"], {"error_type": "ValueError"})
        self.assertNotIn("private", json.dumps(records))
