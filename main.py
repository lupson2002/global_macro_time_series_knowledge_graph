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
import re
import sys
import time
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from tqdm import tqdm

# Import modules from src
from src.ingestion import get_youtube_transcript, fetch_video_ids_from_channel
from src.local_llm_client import LocalLLMClient
from src.exporter import SQLiteExporter, ObsidianMDExporter

# Load environment variables from .env if present
load_dotenv()

# 👑 [Ver 3.1] 76 매크로 채널 풀 (요건 2: Backfill Roster)
# Stage 2 is now deepseek-ai/deepseek-v4-flash via nvidia-api-proxy (localhost:8000).
# Channels are loaded from configs/channels.json — order = throughput priority.
import json
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

def check_processed(db_path: str, video_id: str, include_skipped: bool = True) -> bool:
    """Helper to check if a video_id is already processed in SQLite.

    reports(성공) 또는 skipped_videos(게이트키퍼 스킵) 에 존재하면 True.
    👑 [2026-08-06 H2] 스킵 영상도 영속화되어 다음 크론에서 재수집+재LLM 방지.
    reports 를 우선 조회 — 스킵 후 재처리 성공 시에도 정상 판정.

    `with sqlite3.connect(...)` 로 연결을 컨텍스트 매니저가 관리 — 예외 시에도
    close 보장(이전 try/except 가 conn.close() 를 감싸지 않아 누수 가능했음).
    """
    if not Path(db_path).exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM reports WHERE video_id = ?", (video_id,))
            if cur.fetchone() is not None:
                return True
            if include_skipped:
                cur.execute("SELECT 1 FROM skipped_videos WHERE video_id = ?", (video_id,))
                return cur.fetchone() is not None
            return False
    except Exception:
        # skipped_videos 테이블 미존재(구DB) 시 include_skipped 조회 실패 → 전체 False
        # (보수적: 재수집 허용, 크래시 없음)
        return False


def is_macro_relevant(data: dict) -> bool:
    """추출된 MacroViewSchema dict 가 실제 매크로 분석 가치가 있는지 검사 (게이트키퍼).

    '의미 있는 매크로 신호' 기준 — 아래 중 하나라도 있으면 유지(True):
      - specific_tickers 에 실제 티커 존재 (예: [[NVDA]])
      - 매크로 전술 신호: duration_call / macro_factor / view_time_horizon
      - bull_bear_score 또는 conviction_score 가 중립 5 가 아님

    ⚠️ sector_tilt 는 신호에서 제외 — deepseek-v4-flash 가 홍보/제품 영상에도
    과추출함(예: 식품보존 홍보 → sector_tilt=[[Food Technology]]). 티커·매크로요인·
    듀레이션·시간지평·비중립 점수가 없이 중립 5/5면 홍보/소음으로 스킵.
    (스펙의 '노드 0개 & 신호 0개'만으론 잡히지 않아 보강)
    """
    gn = data.get("graph_nodes", {}) or {}
    tickers = [t for t in (gn.get("specific_tickers") or []) if t]

    qs = data.get("quant_signals", {}) or {}
    bb = qs.get("bull_bear_score")
    conv = qs.get("conviction_score")
    has_tactical = any(qs.get(k) for k in (
        "duration_call", "macro_factor", "view_time_horizon"))

    # 의미 있는 매크로 신호: 실제 티커 OR 매크로 전술 신호 OR 비중립 심리/확신 점수
    has_signal = (
        bool(tickers)
        or has_tactical
        or (bb is not None and bb != 5)
        or (conv is not None and conv != 5)
    )
    return has_signal


def main():
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
    args = parser.parse_args()

    # Resolve paths
    project_dir = Path(__file__).resolve().parent
    db_file_path = project_dir / args.db_path
    vault_dir_path = project_dir / args.vault_dir

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
        sys.exit(1)

    # Collect target Video IDs
    video_targets = []  # List of tuples (video_id, source_name, upload_date)
    
    import datetime
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    # Mode A: Specific Video ID list
    # 👑 upload_date=None — LLM이 transcript에서 추출한 실제 방송일 사용하도록
    # (이전엔 today_str 을 전달 → 실행일이 broadcast_date 로 덮어씌워졌음).
    # --fetch_latest 모드는 RSS pub_date(실제 업로드일)를 전달.
    if args.video_id:
        vids = [vid.strip() for vid in args.video_id.split(",") if vid.strip()]
        for vid in vids:
            video_targets.append((vid, args.source, None))
            
    # Mode B: Fetch latest uploaded videos from RSS feeds (요건 1)
    if args.fetch_latest:
        channels_to_query = {}
        if args.channel_id:
            # Custom channel ID list
            cids = [cid.strip() for cid in args.channel_id.split(",") if cid.strip()]
            for idx, cid in enumerate(cids, 1):
                channels_to_query[f"Custom_Channel_{idx}"] = cid
        else:
            # 👑 [Ver 3.1] Load from configs/channels.json (with --tiers filter)
            try:
                if args.tiers == "all":
                    tier_filter = None
                else:
                    tier_filter = [t.strip() for t in args.tiers.split(",") if t.strip()]
                channels_to_query = load_channels(tier_filter=tier_filter)
            except FileNotFoundError:
                print("⚠️  configs/channels.json not found; falling back to DEFAULT_CHANNELS (7 channels).")
                channels_to_query = DEFAULT_CHANNELS
            
        print(f"📡 Fetching latest RSS feeds from {len(channels_to_query)} channels (max age: {args.max_age_hours}h)...")
        for source_name, channel_id in channels_to_query.items():
            latest_vids_info = fetch_video_ids_from_channel(channel_id, max_age_hours=args.max_age_hours)
            print(f"   - {source_name} ({channel_id}): Found {len(latest_vids_info)} latest videos.")
            for vid, pub_date in latest_vids_info:
                video_targets.append((vid, source_name, pub_date))

    # De-duplicate targets while preserving order
    seen = set()
    unique_targets = []
    for item in video_targets:
        vid = item[0]
        src = item[1]
        pub_date = item[2] if len(item) > 2 else today_str
        if vid not in seen:
            seen.add(vid)
            unique_targets.append((vid, src, pub_date))

    # Backfill mode: jump to DB→MD export without requiring ingestion targets
    if args.backfill_from_db:
        from src.exporter import _load_db_report_as_schema  # local helper
        # 파일명 형식: {Speaker}_{YYYY-MM-DD}_{videoID}.md — 날짜 패턴 기준으로
        # video_id 추출. 이전 rsplit("_",1)[-1] len==11 방식은 video_id 내부에
        # `_` 포함 시(ib-XMy-d_2I) 누락→불필요 재백필+덮어쓰기 버그.
        _id_re = re.compile(r"_(\d{4}-\d{2}-\d{2})_([A-Za-z0-9_-]{11})$")
        existing_videos: set[str] = set()
        for md_path in vault_dir_path.rglob("*.md"):
            m = _id_re.search(md_path.stem)
            if m:
                existing_videos.add(m.group(2))

        backfilled = 0
        skipped = 0
        for schema in _load_db_report_as_schema(str(db_file_path)):
            vid = schema["metadata"]["video_id"]
            if vid in existing_videos:
                skipped += 1
                continue
            try:
                md_path = obsidian_exporter.export_markdown(schema)
                backfilled += 1
                tqdm.write(f"   ✓ Backfilled: {md_path.name}")
            except Exception as e:
                tqdm.write(f"   ❌ Backfill failed for {vid}: {e}")
        print("=" * 60)
        print(f"🏁 Backfill done.  Exported: {backfilled}  Already-on-disk: {skipped}")
        print("=" * 60)
        return

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

        # 👑 [Pre-Ingestion Skip Optimization] (요건 2)
        # Check database before any ingestion or API calls
        if not args.overwrite and check_processed(str(db_file_path), video_id):
            skip_count += 1
            tqdm.write(f"ℹ️ [FAST SKIP] Video '{video_id}' already processed in SQLite. Skipping ingestion.")
            continue

        tqdm.write(f"\n🎬 Processing: {video_id} (Source: {source_channel})")

        # 1. YouTube Ingestion
        try:
            # 👑 [Ver 3.0] Local LLM ⇒ RPD-free. Delay is now configurable.
            if args.ingest_delay > 0 and (success_count + fail_count) > 0:
                tqdm.write(f"   ⏳ Ingest delay: {args.ingest_delay}s...")
                time.sleep(args.ingest_delay)
                
            tqdm.write("   📥 Ingesting transcript from YouTube...")
            transcript = get_youtube_transcript(video_id)
            tqdm.write(f"   ✓ Transcript loaded ({len(transcript):,} characters).")
        except Exception as e:
            err_msg = str(e)
            tqdm.write(f"   ❌ Ingestion failed: {err_msg}")
            fail_count += 1
            
            # 🚨 [YouTube IP 차단 감지 시 조기 중단 방어]
            # 차단 상태에서 계속 찔러서 차단 기간이 영구 누적/연장되는 것을 방지하기 위해 큐를 조기 드롭합니다.
            if "blocking requests from your IP" in err_msg or "blocking your requests" in err_msg or "IPBlocked" in err_msg:
                tqdm.write("⚠️ [CRITICAL] YouTube IP Block (Ban) detected! Aborting remaining pipeline queue to let the IP recover.")
                break
            continue

        # 2. LLM Analysis (Ollama Cloud primary, NIM proxy fallback)
        try:
            if args.llm_delay > 0 and (success_count + fail_count) > 0:
                tqdm.write(f"   ⏳ LLM delay: {args.llm_delay}s...")
                time.sleep(args.llm_delay)
            tqdm.write(f"   🤖 Generating structured macroeconomic view (NIM fallback: {client.model_name})...")
            extracted_data = client.analyze_transcript(transcript, video_id, source_channel=source_channel, upload_date=upload_date)
            tqdm.write("   ✓ Structured JSON generated successfully.")
        except Exception as e:
            # 👑 [2026-08-06 M3] 전체 traceback(수십 줄) 로그 제거 → 단축(예외 타입+메시지).
            tqdm.write(f"   ❌ LLM Analysis failed: {type(e).__name__}: {e}")
            fail_count += 1
            continue

        # 3. Dual Storage Export — 매크로 가치 게이트키퍼 (홍보/소음 영상 스킵)
        try:
            if not is_macro_relevant(extracted_data):
                tqdm.write(f"   ⏭️ [SKIP] 매크로 가치 없는 영상(홍보/소음): {video_id}")
                # 👑 [2026-08-06 H2] 스킵 영상 영속화 → 다음 크론 재수집 방지 (멱등화).
                try:
                    sqlite_exporter.mark_skipped(video_id, reason="not_macro_relevant")
                except Exception:
                    pass
                skip_count += 1
                continue
            tqdm.write("   💾 Exporting to SQLite DB and Obsidian Markdown...")
            sqlite_exporter.export_data(extracted_data)
            md_path = obsidian_exporter.export_markdown(extracted_data)
            tqdm.write(f"   ✓ Saved Markdown to: {md_path.name}")
            success_count += 1
        except Exception as e:
            tqdm.write(f"   ❌ Dual Storage Export failed: {e}")
            fail_count += 1
            continue

    print("=" * 60)
    print("🏁 Pipeline run finished.")
    print(f"   Processed: {success_count} | Skipped: {skip_count} | Failed: {fail_count}")
    print("=" * 60)

if __name__ == "__main__":
    main()
