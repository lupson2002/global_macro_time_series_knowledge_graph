import json
import tempfile
import unittest
from pathlib import Path

from scripts import summarize_run_events
from src.event_summary import format_run_summary, read_journal, summarize_runs


def _event(kind: str, run_id: str, timestamp: str, **extra) -> dict:
    return {
        "schema_version": 1,
        "event": kind,
        "status": extra.pop("status", "running"),
        "run_id": run_id,
        "timestamp": timestamp,
        "details": extra.pop("details", {}),
        **extra,
    }


class EventSummaryTests(unittest.TestCase):
    def test_read_journal_isolates_malformed_and_unknown_schema_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            lines = [
                json.dumps(_event("run.started", "a", "2026-08-07T00:00:00+00:00")),
                "not-json",
                json.dumps({"schema_version": 2, "event": "run.started"}),
            ]
            path.write_text("\n".join(lines), encoding="utf-8")

            result = read_journal(path)

        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.malformed_lines, 2)

    def test_summaries_are_newest_first_and_keep_run_counts(self):
        events = [
            _event("run.started", "old", "2026-08-07T00:00:00+00:00", details={"mode": "pipeline"}),
            _event("video.started", "old", "2026-08-07T00:00:01+00:00"),
            _event("video.finished", "old", "2026-08-07T00:00:02+00:00", status="success"),
            _event("run.finished", "old", "2026-08-07T00:00:03+00:00", status="success",
                   details={"processed": 1, "skipped": 0, "failed": 0}),
            _event("run.started", "new", "2026-08-08T00:00:00+00:00", details={"mode": "report"}),
            _event("report.started", "new", "2026-08-08T00:00:01+00:00", report="daily"),
        ]

        summaries = summarize_runs(events)

        self.assertEqual([item["run_id"] for item in summaries], ["new", "old"])
        self.assertEqual(summaries[0]["status"], "running")
        self.assertEqual(summaries[0]["report"], "daily")
        self.assertEqual(summaries[1]["processed"], 1)
        self.assertIn("videos=1/1", format_run_summary(summaries[1]))

    def test_cli_json_reports_data_quality_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                json.dumps(_event("run.started", "a", "2026-08-07T00:00:00+00:00")) + "\nbad\n",
                encoding="utf-8",
            )
            exit_code = summarize_run_events.main(["--event-log", str(path), "--json"])

        self.assertEqual(exit_code, 2)

    def test_cli_returns_one_for_absent_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            exit_code = summarize_run_events.main(
                ["--event-log", str(Path(directory) / "absent.jsonl")]
            )
        self.assertEqual(exit_code, 1)


class WrapperEventLogContractTests(unittest.TestCase):
    def test_operational_wrappers_use_opt_in_event_log_array(self):
        root = Path(__file__).resolve().parent.parent
        wrappers = [
            "run_frequent.sh",
            "run_morning_report.sh",
            "run_insight_report.sh",
            "run_weekly_orchestrator.sh",
            "run_market_narrative_report.sh",
        ]
        for name in wrappers:
            with self.subTest(wrapper=name):
                text = (root / name).read_text(encoding="utf-8")
                self.assertIn('if [[ -n "${PIPELINE_EVENT_LOG:-}" ]]', text)
                self.assertIn('EVENT_LOG_ARGS=(--event-log "${PIPELINE_EVENT_LOG}")', text)
                self.assertIn('"${EVENT_LOG_ARGS[@]}"', text)
                self.assertIn('exit "${EXIT_CODE}"', text)
