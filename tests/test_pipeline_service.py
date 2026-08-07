import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, call, patch

from src.pipeline import (
    PipelineResult,
    PipelineService,
    PipelineStage,
    PipelineStatus,
    VideoTarget,
)


def relevant_view(video_id="abcdefghijk"):
    return {
        "metadata": {"video_id": video_id},
        "graph_nodes": {"specific_tickers": ["[[TLT]]"]},
        "quant_signals": {"bull_bear_score": 5, "conviction_score": 5},
    }


class PipelineServiceTests(unittest.TestCase):
    def make_service(self, db_path, *, ingest=None, analyze=None, relevant=True):
        llm = Mock()
        llm.analyze_transcript.side_effect = analyze
        if analyze is None:
            llm.analyze_transcript.return_value = relevant_view()
        sqlite_exporter = Mock()
        obsidian_exporter = Mock()
        obsidian_exporter.export_markdown.return_value = Path("view.md")
        service = PipelineService(
            db_path=str(db_path),
            llm_client=llm,
            sqlite_exporter=sqlite_exporter,
            obsidian_exporter=obsidian_exporter,
            ingest=ingest or (lambda _: "full transcript"),
            relevance_check=lambda _: relevant,
        )
        return service, llm, sqlite_exporter, obsidian_exporter

    def test_precheck_skip_avoids_ingestion_and_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            ingest = Mock(return_value="should not run")
            service, llm, _, _ = self.make_service(
                Path(directory) / "existing.db", ingest=ingest
            )
            with patch("src.pipeline.check_processed", return_value=True):
                result = service.process(VideoTarget("abcdefghijk", "Channel"))
        self.assertEqual(
            (result.status, result.stage),
            (PipelineStatus.SKIPPED, PipelineStage.PRECHECK),
        )
        ingest.assert_not_called()
        llm.analyze_transcript.assert_not_called()

    def test_success_passes_complete_transcript_and_returns_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            service, llm, sqlite_exporter, obsidian_exporter = self.make_service(
                Path(directory) / "missing.db",
                ingest=lambda _: "HEAD" + ("middle " * 10_000) + "TAIL",
            )
            target = VideoTarget("abcdefghijk", "Channel", "2026-08-07")
            result = service.process(target)

        self.assertEqual(result.status, PipelineStatus.SUCCESS)
        self.assertEqual(result.stage, PipelineStage.STORAGE)
        transcript = llm.analyze_transcript.call_args.args[0]
        self.assertTrue(transcript.startswith("HEAD"))
        self.assertTrue(transcript.endswith("TAIL"))
        self.assertEqual(result.transcript_chars, len(transcript))
        self.assertEqual(result.markdown_path, Path("view.md"))
        obsidian_exporter.export_markdown.assert_called_once()
        sqlite_exporter.export_data.assert_called_once()

    def test_irrelevant_view_is_persisted_as_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, sqlite_exporter, obsidian_exporter = self.make_service(
                Path(directory) / "missing.db", relevant=False
            )
            result = service.process(VideoTarget("abcdefghijk", "Channel"))
        self.assertEqual(
            (result.status, result.stage),
            (PipelineStatus.SKIPPED, PipelineStage.RELEVANCE),
        )
        sqlite_exporter.mark_skipped.assert_called_once_with(
            "abcdefghijk", reason="not_macro_relevant"
        )
        obsidian_exporter.export_markdown.assert_not_called()

    def test_ip_block_aborts_queue_at_ingestion_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            service, llm, _, _ = self.make_service(
                Path(directory) / "missing.db",
                ingest=Mock(side_effect=RuntimeError("IPBlocked by YouTube")),
            )
            result = service.process(VideoTarget("abcdefghijk", "Channel"))
        self.assertTrue(result.abort_queue)
        self.assertEqual(result.stage, PipelineStage.INGESTION)
        llm.analyze_transcript.assert_not_called()

    def test_analysis_failure_is_typed_and_does_not_store(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, sqlite_exporter, obsidian_exporter = self.make_service(
                Path(directory) / "missing.db", analyze=RuntimeError("provider offline")
            )
            result = service.process(VideoTarget("abcdefghijk", "Channel"))
        self.assertEqual(
            (result.status, result.stage),
            (PipelineStatus.FAILED, PipelineStage.ANALYSIS),
        )
        self.assertIn("RuntimeError: provider offline", result.message)
        sqlite_exporter.export_data.assert_not_called()
        obsidian_exporter.export_markdown.assert_not_called()

    def test_markdown_failure_keeps_database_uncommitted(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, sqlite_exporter, obsidian_exporter = self.make_service(
                Path(directory) / "missing.db"
            )
            obsidian_exporter.export_markdown.side_effect = OSError("disk full")
            result = service.process(VideoTarget("abcdefghijk", "Channel"))
        self.assertEqual(
            (result.status, result.stage),
            (PipelineStatus.FAILED, PipelineStage.STORAGE),
        )
        sqlite_exporter.export_data.assert_not_called()

    def test_database_failure_reports_partial_markdown_artifact_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, sqlite_exporter, _ = self.make_service(
                Path(directory) / "missing.db"
            )
            sqlite_exporter.export_data.side_effect = OSError("database locked")
            result = service.process(VideoTarget("abcdefghijk", "Channel"))
        self.assertEqual(
            (result.status, result.stage),
            (PipelineStatus.FAILED, PipelineStage.STORAGE),
        )
        self.assertEqual(result.markdown_path, Path("view.md"))
        self.assertEqual(result.warnings, ("markdown_saved_database_pending",))

    def test_configured_delays_apply_only_after_prior_work(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _, _ = self.make_service(Path(directory) / "missing.db")
            service.sleep = Mock()
            target = VideoTarget("abcdefghijk", "Channel")
            service.process(target, apply_delays=False, ingest_delay=2, llm_delay=3)
            service.sleep.assert_not_called()
            service.process(
                target, overwrite=True, apply_delays=True, ingest_delay=2, llm_delay=3
            )
            self.assertEqual(service.sleep.call_args_list, [call(2), call(3)])


class PipelineCliExitTests(unittest.TestCase):
    def test_video_failure_returns_nonzero_process_status(self):
        import main as pipeline_cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = VideoTarget("abcdefghijk", "Channel")
            failed = PipelineResult(
                target,
                PipelineStatus.FAILED,
                PipelineStage.ANALYSIS,
                "provider offline",
            )
            service = Mock()
            service.process.return_value = failed
            argv = [
                "main.py",
                "--video_id",
                target.video_id,
                "--db_path",
                str(root / "macro.db"),
                "--vault_dir",
                str(root / "vault"),
            ]
            with patch.object(sys, "argv", argv), patch.object(
                pipeline_cli, "PipelineService", return_value=service
            ):
                exit_code = pipeline_cli.main()
        self.assertEqual(exit_code, 1)
