import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, call, patch

import main as pipeline_cli
from main import build_parser, collect_video_targets, run_backfill
from src.pipeline import PipelineResult, PipelineStage, PipelineStatus, VideoTarget
from src.projections import LanceDbProjection


class MainCliCharacterizationTests(unittest.TestCase):
    def test_parser_preserves_public_defaults(self):
        args = build_parser().parse_args([])

        self.assertEqual(args.vault_dir, "obsidian_vault")
        self.assertEqual(args.db_path, "data/macro_knowledge.db")
        self.assertEqual(args.source, "CNBC_Bloomberg")
        self.assertEqual(args.tiers, "all")
        self.assertEqual(args.max_videos, 0)
        self.assertFalse(args.overwrite)
        self.assertIsNone(args.event_log)

    def test_main_wires_an_explicit_lancedb_projection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            pipeline_cli, "PipelineService"
        ) as service_class, patch.object(pipeline_cli, "LocalLLMClient"), patch.object(
            pipeline_cli, "SQLiteExporter"
        ), patch.object(pipeline_cli, "ObsidianMDExporter"):
            exit_code = pipeline_cli.main(
                [
                    "--db_path",
                    str(Path(directory) / "macro.db"),
                    "--vault_dir",
                    str(Path(directory) / "vault"),
                ]
            )

        self.assertEqual(exit_code, 0)
        projection = service_class.call_args.kwargs["vector_projection"]
        self.assertIsInstance(projection, LanceDbProjection)

    def test_target_collection_preserves_manual_priority_and_deduplicates(self):
        args = Namespace(
            video_id="manual00001, shared00001",
            source="Manual",
            fetch_latest=True,
            channel_id="UC-ONE",
            tiers="all",
            max_age_hours=24,
        )
        fetch = Mock(
            return_value=[
                ("shared00001", "2026-08-06"),
                ("fetched0001", "2026-08-07"),
            ]
        )

        targets = collect_video_targets(args, fetch=fetch)

        self.assertEqual(
            targets,
            [
                ("manual00001", "Manual", None),
                ("shared00001", "Manual", None),
                ("fetched0001", "Custom_Channel_1", "2026-08-07"),
            ],
        )
        fetch.assert_called_once_with("UC-ONE", max_age_hours=24)

    def test_target_collection_passes_selected_tiers_to_channel_loader(self):
        args = Namespace(
            video_id=None,
            source="Manual",
            fetch_latest=True,
            channel_id=None,
            tiers="tier_a,tier_b",
            max_age_hours=0,
        )
        load = Mock(return_value={"Channel": "UC-ID"})
        fetch = Mock(return_value=[("abcdefghijk", "2026-08-07")])

        targets = collect_video_targets(args, fetch=fetch, channel_loader=load)

        load.assert_called_once_with(tier_filter=["tier_a", "tier_b"])
        self.assertEqual(targets, [("abcdefghijk", "Channel", "2026-08-07")])

    def test_backfill_recognizes_video_ids_containing_underscores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault"
            vault.mkdir()
            (vault / "Speaker_2026-08-07_ib-XMy-d_2I.md").write_text(
                "existing", encoding="utf-8"
            )
            schemas = [
                {"metadata": {"video_id": "ib-XMy-d_2I"}},
                {"metadata": {"video_id": "abcdefghijk"}},
            ]
            exporter = Mock()
            exporter.export_markdown.return_value = Path("new.md")

            exit_code = run_backfill(root / "macro.db", vault, exporter, schemas)

        self.assertEqual(exit_code, 0)
        exporter.export_markdown.assert_called_once_with(schemas[1])

    def test_backfill_returns_nonzero_when_any_export_does_not_complete(self):
        schema = {"metadata": {"video_id": "abcdefghijk"}}
        exporter = Mock()
        exporter.export_markdown.side_effect = OSError("disk full")
        with tempfile.TemporaryDirectory() as directory:
            exit_code = run_backfill(
                Path(directory) / "macro.db",
                Path(directory) / "vault",
                exporter,
                [schema],
            )
        self.assertEqual(exit_code, 1)

    def test_main_preserves_target_order_delay_rule_and_failure_exit(self):
        targets = [
            VideoTarget("abcdefghijk", "Manual"),
            VideoTarget("lmnopqrstuv", "Manual"),
            VideoTarget("12345678901", "Manual"),
        ]
        service = Mock()
        service.process.side_effect = [
            PipelineResult(targets[0], PipelineStatus.SKIPPED, PipelineStage.PRECHECK),
            PipelineResult(
                targets[1],
                PipelineStatus.SUCCESS,
                PipelineStage.STORAGE,
                markdown_path=Path("saved.md"),
            ),
            PipelineResult(targets[2], PipelineStatus.FAILED, PipelineStage.ANALYSIS),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(
            pipeline_cli, "PipelineService", return_value=service
        ), patch.object(pipeline_cli, "LocalLLMClient"), patch.object(
            pipeline_cli, "SQLiteExporter"
        ), patch.object(pipeline_cli, "ObsidianMDExporter"):
            exit_code = pipeline_cli.main(
                [
                    "--video_id",
                    ",".join(target.video_id for target in targets),
                    "--source",
                    "Manual",
                    "--db_path",
                    str(Path(directory) / "macro.db"),
                    "--vault_dir",
                    str(Path(directory) / "vault"),
                    "--ingest_delay",
                    "2",
                    "--llm_delay",
                    "3",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            service.process.call_args_list,
            [
                call(
                    targets[0], overwrite=False, apply_delays=False,
                    ingest_delay=2.0, llm_delay=3.0,
                ),
                call(
                    targets[1], overwrite=False, apply_delays=False,
                    ingest_delay=2.0, llm_delay=3.0,
                ),
                call(
                    targets[2], overwrite=False, apply_delays=True,
                    ingest_delay=2.0, llm_delay=3.0,
                ),
            ],
        )

    def test_main_writes_opt_in_run_and_video_events(self):
        target = VideoTarget("abcdefghijk", "Manual")
        service = Mock()
        service.process.return_value = PipelineResult(
            target,
            PipelineStatus.SUCCESS,
            PipelineStage.STORAGE,
            transcript_chars=321,
            markdown_path=Path("saved.md"),
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            pipeline_cli, "PipelineService", return_value=service
        ), patch.object(pipeline_cli, "LocalLLMClient"), patch.object(
            pipeline_cli, "SQLiteExporter"
        ), patch.object(pipeline_cli, "ObsidianMDExporter"):
            event_log = Path(directory) / "events.jsonl"
            exit_code = pipeline_cli.main(
                [
                    "--video_id", target.video_id,
                    "--source", target.source_channel,
                    "--db_path", str(Path(directory) / "macro.db"),
                    "--vault_dir", str(Path(directory) / "vault"),
                    "--event_log", str(event_log),
                ]
            )
            records = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [record["event"] for record in records],
            ["run.started", "video.started", "video.finished", "run.finished"],
        )
        self.assertEqual(len({record["run_id"] for record in records}), 1)
        self.assertEqual(records[2]["details"]["transcript_chars"], 321)
        self.assertEqual(records[-1]["details"]["processed"], 1)
