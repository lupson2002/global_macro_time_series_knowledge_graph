# -*- coding: utf-8 -*-
"""
Main Orchestrator CLI for Global Macro Time-Series Knowledge Graph
===============================================================
Runs E2E pipeline: Ingestion -> LLM Analysis -> Dual Export.
Supports fetching latest video IDs from YouTube channel RSS feeds.

Usage:
  # Process specific video IDs
  .venv/bin/python main.py --video_id uMMwAbYSmr4 --source CNBC_Bloomberg
  
  # Fetch and process latest videos from default macro channels
  .venv/bin/python main.py --fetch_latest
"""

import argparse
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from tqdm import tqdm

# Import modules from src
from src.ingestion import fetch_video_ids_from_channel
from src.local_llm_client import LocalLLMClient
from src.exporter import SQLiteExporter, ObsidianMDExporter
from src.pipeline import (
    PipelineService,
    PipelineStage,
    PipelineStatus,
    VideoTarget,
    check_processed,
    is_macro_relevant,
)
from src.projections import LanceDbProjection
from src.run_events import RunJournal

# 👑 [Ver 3.1] 76 매크로 채널 풀 (요건 2: Backfill Roster)
# Stage 2 is now deepseek-ai/deepseek-v4-flash via nvidia-api-proxy (localhost:8000).
# Channels are loaded from configs/channels.json — order = throughput priority.
_DEFAULT_CHANNELS_PATH = Path(__file__).resolve().parent / "configs" / "channels.json"

def load_channels(
    path: Path = _DEFAULT_CHANNELS_PATH,
    tier_filter: list[str] | None = None,
    include_disabled: bool = False,
) -> dict[str, str]:
    """Load channel roster from JSON.  Returns ordered dict {name: channel_id}.

    Args:
        path: channels.json location
        tier_filter: If given, only include these tier keys (e.g. ["tier_1_highest_density"]).
                     None = include all tiers in priority order.
        include_disabled: If False (default), skip tiers with `"_enabled": false`.
                          Set True for validation / audit purposes.
    """
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out: dict[str, str] = {}
    for tier_key, tier_data in cfg.get("tiers", {}).items():
        if tier_filter and tier_key not in tier_filter:
            continue
        if not include_disabled and tier_data.get("_enabled", True) is False:
            # Tier explicitly disabled (e.g. unverified channel_ids) — skip.
            continue
        for ch in tier_data.get("channels", []):
            name = ch["name"]
            # Avoid name collisions: append tier suffix
            if name in out:
                name = f"{name}_{tier_key}"
            out[name] = ch["channel_id"]
    return out

# Fallback minimal set (used if channels.json missing)
DEFAULT_CHANNELS = {
    "Wealthion": "UCKMeK-HGHfUFFArZ91rzv5A",
    "Bloomberg_Markets_Finance": "UCIALMKvObZNtJ6AmdCLP7Lg",
    "Bloomberg_Podcasts": "UChF5O40UBqAc82I7-i5ig6A",
    "Bloomberg_Technology": "UCrM7B7SL_g1edFOnmj-SDKg",
    "Real_Vision": "UCwSVtQvURxiyn1CQeyoExZg",
    "CNBC": "UCvJJ_dzjViJCoLf5uKUTwoA",
    "Yahoo_Finance": "UCxZG-dvg0cLQsgCln7DBHKw",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI without reading process arguments."""
    parser = argparse.ArgumentParser(description="Global Macro Time-Series Knowledge Graph Pipeline (Ver 3.0)")
    parser.add_argument("--video_id", help="YouTube Video ID or comma-separated list of IDs")
    parser.add_argument("--channel_id", help="YouTube Channel ID or comma-separated list of IDs")
    parser.add_argument("--fetch_latest", action="store_true", help="Fetch latest video IDs from target channels RSS feeds")
    parser.add_argument("--max_age_hours", type=int, default=0,
                        help="Maximum age of RSS videos to fetch in hours (0 = no filter, 720=30d, 1440=60d for backfill)")
    parser.add_argument("--vault_dir", default="obsidian_vault", help="Obsidian Vault directory path")
    parser.add_argument("--db_path", default="data/macro_knowledge.db", help="SQLite database path")
    parser.add_argument("--source", default="CNBC_Bloomberg", help="Default source channel name tag")
    parser.add_argument("--overwrite", action="store_true", help="Reprocess even if already completed")
    # 👑 [Ver 3.0] Ver 3.0: Quota-free local inference ⇒ configurable delays (default 0 for batch backfill)
    parser.add_argument("--ingest_delay", type=float, default=0.0,
                        help="Seconds to wait between YouTube ingestion calls (default 0.0; Y700 local engine, no RPD constraint)")
    parser.add_argument("--llm_delay", type=float, default=0.0,
                        help="Seconds to wait between NIM LLM calls (default 0.0; proxy handles rate-limit via 6-key rotation)")
    parser.add_argument("--max_videos", type=int, default=0,
                        help="Cap videos processed per run (0 = unlimited, useful for backfill chunks)")
    parser.add_argument("--backfill_from_db", action="store_true",
                        help="[Ver 3.1] Export existing DB reports to Obsidian MD files (skips already-exported videos).")
    parser.add_argument("--tiers", default="all",
                        help="[Ver 3.1] Comma-separated tier names to include (e.g. 'tier_1_highest_density,tier_3_macro_independent') or 'all'. Default 'all'.")
    parser.add_argument(
        "--event-log", "--event_log", dest="event_log",
        help="Append opt-in run/video lifecycle events to this JSONL file",
    )
    return parser


def collect_video_targets(
    args: argparse.Namespace,
    *,
    fetch=fetch_video_ids_from_channel,
    channel_loader=load_channels,
) -> list[tuple[str, str, str | None]]:
    """Collect and de-duplicate manual/RSS targets while preserving priority."""
    video_targets: list[tuple[str, str, str | None]] = []
    if args.video_id:
        vids = [vid.strip() for vid in args.video_id.split(",") if vid.strip()]
        video_targets.extend((vid, args.source, None) for vid in vids)

    if args.fetch_latest:
        if args.channel_id:
            cids = [cid.strip() for cid in args.channel_id.split(",") if cid.strip()]
            channels_to_query = {
                f"Custom_Channel_{idx}": channel_id
                for idx, channel_id in enumerate(cids, 1)
            }
        else:
            try:
                tier_filter = None if args.tiers == "all" else [
                    tier.strip() for tier in args.tiers.split(",") if tier.strip()
                ]
                channels_to_query = channel_loader(tier_filter=tier_filter)
            except FileNotFoundError:
                print("⚠️  configs/channels.json not found; falling back to DEFAULT_CHANNELS (7 channels).")
                channels_to_query = DEFAULT_CHANNELS

        print(f"📡 Fetching latest RSS feeds from {len(channels_to_query)} channels (max age: {args.max_age_hours}h)...")
        for source_name, channel_id in channels_to_query.items():
            latest_videos = fetch(channel_id, max_age_hours=args.max_age_hours)
            print(f"   - {source_name} ({channel_id}): Found {len(latest_videos)} latest videos.")
            video_targets.extend(
                (video_id, source_name, pub_date)
                for video_id, pub_date in latest_videos
            )

    seen: set[str] = set()
    unique_targets: list[tuple[str, str, str | None]] = []
    for video_id, source_name, upload_date in video_targets:
        if video_id not in seen:
            seen.add(video_id)
            unique_targets.append((video_id, source_name, upload_date))
    return unique_targets


def run_backfill(
    db_file_path: Path,
    vault_dir_path: Path,
    obsidian_exporter: ObsidianMDExporter,
    schemas: Iterable[dict] | None = None,
) -> int:
    """Regenerate absent Markdown projections and return a process exit code."""
    if schemas is None:
        from src.exporter import _load_db_report_as_schema

        schemas = _load_db_report_as_schema(str(db_file_path))
    id_pattern = re.compile(r"_(\d{4}-\d{2}-\d{2})_([A-Za-z0-9_-]{11})$")
    existing_videos: set[str] = set()
    for md_path in vault_dir_path.rglob("*.md"):
        match = id_pattern.search(md_path.stem)
        if match:
            existing_videos.add(match.group(2))

    backfilled = 0
    skipped = 0
    backfill_failed = 0
    for schema in schemas:
        video_id = schema["metadata"]["video_id"]
        if video_id in existing_videos:
            skipped += 1
            continue
        try:
            md_path = obsidian_exporter.export_markdown(schema)
            backfilled += 1
            tqdm.write(f"   ✓ Backfilled: {md_path.name}")
        except Exception as exc:  # noqa: BLE001 - continue remaining projections
            backfill_failed += 1
            tqdm.write(f"   ❌ Backfill failed for {video_id}: {exc}")
    print("=" * 60)
    print(f"🏁 Backfill done.  Exported: {backfilled}  Already-on-disk: {skipped}")
    print("=" * 60)
    return 1 if backfill_failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Resolve paths
    project_dir = Path(__file__).resolve().parent
    db_file_path = project_dir / args.db_path
    vault_dir_path = project_dir / args.vault_dir
    event_log_path = project_dir / args.event_log if args.event_log else None
    events = RunJournal.from_path(
        event_log_path,
        warn=lambda message: tqdm.write(f"   ⚠️ {message}"),
    )
    events.run_started("backfill" if args.backfill_from_db else "pipeline")

    # Ensure directories exist
    db_file_path.parent.mkdir(parents=True, exist_ok=True)
    vault_dir_path.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("🌐 Global Macro Knowledge Graph Pipeline")
    print(f"   Database:   {db_file_path}")
    print(f"   Vault:      {vault_dir_path}")
    print(f"   Overwrite:  {args.overwrite}")
    print("=" * 60)

    # Tier 1 delegates to cloud_client: Ollama Cloud first, NIM proxy fallback.

    # Initialize Clients
    try:
        client = LocalLLMClient()
        sqlite_exporter = SQLiteExporter(str(db_file_path))
        obsidian_exporter = ObsidianMDExporter(str(vault_dir_path))
    except Exception as err:
        print(f"❌ Initialization failure: {err}")
        events.run_finished(1, stage="initialization")
        return 1
    pipeline = PipelineService(
        db_path=str(db_file_path),
        llm_client=client,
        sqlite_exporter=sqlite_exporter,
        obsidian_exporter=obsidian_exporter,
        vector_projection=LanceDbProjection(),
    )

    unique_targets = collect_video_targets(args)

    # Backfill mode: jump to DB→MD export without requiring ingestion targets
    if args.backfill_from_db:
        exit_code = run_backfill(
            db_file_path, vault_dir_path, obsidian_exporter
        )
        events.run_finished(exit_code, stage="backfill")
        return exit_code

    # Counters
    success_count = 0
    skip_count = 0
    fail_count = 0

    # 👑 [Ver 3.0] Cap for chunked backfill runs
    if args.max_videos and len(unique_targets) > args.max_videos:
        tqdm.write(f"⚙️  --max_videos={args.max_videos}: processing first {args.max_videos} of {len(unique_targets)}")
        unique_targets = unique_targets[:args.max_videos]

    # 👑 [2026-08-06 M3] cron(비-TTY)에서 tqdm 진행바 비활성 — \r 진행선이
    # 2>&1 로그에 매 틱마다 새 줄로 기록되어 pipeline_cron.log ~1MB/일 폭증.
    pbar = tqdm(unique_targets, desc="Processing Pipeline", disable=not sys.stderr.isatty())
    for video_id, source_channel, upload_date in pbar:
        pbar.set_postfix_str(f"Success: {success_count} | Skip: {skip_count} | Fail: {fail_count}")
        tqdm.write(f"\n🎬 Processing: {video_id} (Source: {source_channel})")
        events.video_started(video_id, source_channel)
        result = pipeline.process(
            VideoTarget(video_id, source_channel, upload_date),
            overwrite=args.overwrite,
            apply_delays=(success_count + fail_count) > 0,
            ingest_delay=args.ingest_delay,
            llm_delay=args.llm_delay,
        )
        events.video_finished(
            video_id, source_channel, result.status.value, result.stage.value,
            abort_queue=result.abort_queue,
            transcript_chars=result.transcript_chars,
            warning_count=len(result.warnings),
        )
        if result.status is PipelineStatus.SUCCESS:
            success_count += 1
            tqdm.write(
                f"   ✓ Processed {result.transcript_chars:,} transcript chars; "
                f"saved Markdown to: {result.markdown_path.name}"
            )
        elif result.status is PipelineStatus.SKIPPED:
            skip_count += 1
            label = "FAST SKIP" if result.stage is PipelineStage.PRECHECK else "SKIP"
            tqdm.write(f"   ⏭️ [{label}] {video_id}: {result.message}")
        else:
            fail_count += 1
            tqdm.write(f"   ❌ {result.stage.value} failed: {result.message}")
            if result.abort_queue:
                tqdm.write(
                    "⚠️ [CRITICAL] YouTube IP block detected; aborting remaining queue."
                )
                break
        for warning in result.warnings:
            tqdm.write(f"   ⚠️ {warning}")

    print("=" * 60)
    print("🏁 Pipeline run finished.")
    print(f"   Processed: {success_count} | Skipped: {skip_count} | Failed: {fail_count}")
    print("=" * 60)
    exit_code = 1 if fail_count else 0
    events.run_finished(
        exit_code,
        counts={
            "processed": success_count,
            "skipped": skip_count,
            "failed": fail_count,
        },
    )
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
