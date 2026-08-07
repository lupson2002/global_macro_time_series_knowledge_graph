import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts import insight_report
from scripts.insights import run_market_narrative
from src import orchestrator, report_generator


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ReportCliEventTests(unittest.TestCase):
    def assert_lifecycle(self, records: list[dict], report: str, status: str = "success"):
        self.assertEqual(
            [record["event"] for record in records],
            ["run.started", "report.started", "report.finished", "run.finished"],
        )
        self.assertEqual(records[1]["report"], report)
        self.assertEqual(records[2]["status"], status)
        self.assertEqual(len({record["run_id"] for record in records}), 1)

    def test_daily_cli_emits_opt_in_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            report_generator, "generate_morning_report"
        ):
            path = Path(directory) / "daily.jsonl"
            exit_code = report_generator.main(["--event-log", str(path)])

            self.assertEqual(exit_code, 0)
            self.assert_lifecycle(_records(path), "daily")

    def test_insight_cli_emits_opt_in_lifecycle(self):
        summary = {"nodes": 1, "edges": 2, "communities": 3, "rag_queries": 4}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            output.write_text("report", encoding="utf-8")
            path = Path(directory) / "insight.jsonl"
            with patch.object(insight_report, "build_report", return_value=("body", summary)), \
                 patch.object(insight_report, "write_report_artifact", return_value=output):
                insight_report.main(["--no-send", "--event-log", str(path)])

            self.assert_lifecycle(_records(path), "insight")

    def test_narrative_cli_emits_opt_in_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.md"
            vault = root / "vault.md"
            report.write_text("report", encoding="utf-8")
            vault.write_text("vault", encoding="utf-8")
            path = root / "narrative.jsonl"
            with patch.object(run_market_narrative, "generate_narrative_report", return_value="body"), \
                 patch.object(run_market_narrative, "save_outputs", return_value=(report, vault)):
                run_market_narrative.main(["--no-send", "--event-log", str(path)])

            self.assert_lifecycle(_records(path), "narrative")

    def test_cio_preserves_swallowed_exception_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            orchestrator, "aggregate_macro_context", new=AsyncMock(side_effect=RuntimeError("offline"))
        ):
            path = Path(directory) / "cio.jsonl"
            asyncio.run(orchestrator.run_orchestrator(path))

            records = _records(path)
            self.assert_lifecycle(records, "cio", "failed")
            self.assertEqual(records[2]["stage"], "aggregation")
            self.assertEqual(records[2]["details"], {"error_type": "RuntimeError"})

    def test_report_clis_do_not_create_a_default_event_file(self):
        with patch.object(report_generator, "generate_morning_report"):
            self.assertEqual(report_generator.main([]), 0)

    def test_daily_preserves_nonzero_failure_exit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            report_generator, "generate_morning_report", side_effect=RuntimeError("offline")
        ):
            path = Path(directory) / "daily-failure.jsonl"
            exit_code = report_generator.main(["--event-log", str(path)])

            self.assertEqual(exit_code, 1)
            self.assert_lifecycle(_records(path), "daily", "failed")

    def test_insight_and_narrative_preserve_exception_propagation(self):
        cases = (
            (insight_report.main, insight_report, "build_report", "insight"),
            (run_market_narrative.main, run_market_narrative, "generate_narrative_report", "narrative"),
        )
        for main, module, target, report in cases:
            with self.subTest(report=report), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"{report}-failure.jsonl"
                with patch.object(module, target, side_effect=RuntimeError("private detail")):
                    with self.assertRaisesRegex(RuntimeError, "private detail"):
                        main(["--no-send", "--event-log", str(path)])
                records = _records(path)
                self.assert_lifecycle(records, report, "failed")
                self.assertNotIn("private detail", json.dumps(records))
