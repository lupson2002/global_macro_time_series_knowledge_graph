# -*- coding: utf-8 -*-
"""
Custom MCP Server for Global Macro Time-Series Knowledge Graph
============================================================
Exposes 8 specialized tools to LLM clients (Claude, Cursor, etc.).
- Fully async I/O using aiosqlite and aiofiles.
- Strictly enforces Read-Only SQLite mode for security.
- Truncation safeguards on document reads to prevent context overflow.
- Highly efficient node adjacency queries using relational DB JOINs.
"""

import re
import json
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any
import aiosqlite
import aiofiles
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("Macro_Wiki_Analyst")

# ---------------------------------------------------------------------------
# Path Resolution (Auto-detected based on project root directory)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "macro_knowledge.db"
VAULT_PATH = PROJECT_ROOT / "obsidian_vault"

# Build URI for SQLite Read-Only enforcement
DB_URI = f"file:{DB_PATH.as_posix()}?mode=ro"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
async def query_db_async(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Helper to query the SQLite database asynchronously in Read-Only mode."""
    if not DB_PATH.exists():
        return []
    
    results = []
    # Force URI=True and read-only mode to prevent any modification attempts
    # 👑 [2026-08-06 M2] busy_timeout — 쓰기 트랜잭션(WAL checkpoint)과 충돌 시
    # 즉시 실패 대신 10s 대기. query_only 는 mode=ro 와 중복되는 belt-and-suspenders.
    async with aiosqlite.connect(DB_URI, uri=True) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute("PRAGMA query_only=ON")
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            async for row in cursor:
                results.append(dict(row))
    return results

# ---------------------------------------------------------------------------
# 1. get_recent_reports
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_recent_reports(limit: Optional[int] = None) -> str:
    """
    Retrieves the ingested macro views, metadata, and core theses.
    If limit is specified, returns up to limit reports, otherwise returns all.
    """
    query = """
        SELECT r.video_id, r.speaker_name, r.speaker_role, r.source_channel,
               r.broadcast_date, r.time_box, r.core_thesis, r.verbatim_quote,
               r.conditional_catalysts, r.invalidation_risks,
               r.key_data_points, r.additional_quotes, r.price_targets, r.speaker_institution,
               r.expectation_gap, r.causal_chain, r.tracking_indicators, r.tactical_stance,
               q.bull_bear_score, q.conviction_score, q.contrarian_flag,
               q.sector_tilt, q.duration_call, q.macro_factor, q.view_time_horizon
        FROM reports r
        LEFT JOIN quant_signals q ON r.video_id = q.video_id
        ORDER BY r.created_at DESC
    """
    params = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    try:
        rows = await query_db_async(query, params)
        if not rows:
            return "No recent reports found in the database."
        return json.dumps(rows, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error retrieving recent reports: {e}"

# ---------------------------------------------------------------------------
# 2. get_speaker_views
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_speaker_views(speaker_name: str) -> str:
    """
    Retrieves all views, conviction scores, and opinions associated with a specific speaker.
    Allows LLM to analyze the consistency and conviction trend of a target macro expert.
    """
    query = """
        SELECT r.video_id, r.source_channel, r.broadcast_date, r.time_box, 
               r.core_thesis, r.verbatim_quote,
               q.bull_bear_score, q.conviction_score, q.contrarian_flag
        FROM reports r
        LEFT JOIN quant_signals q ON r.video_id = q.video_id
        WHERE r.speaker_name LIKE ?
        ORDER BY r.broadcast_date DESC
    """
    try:
        rows = await query_db_async(query, (f"%{speaker_name}%",))
        if not rows:
            return f"No views found for speaker: '{speaker_name}'."
        return json.dumps(rows, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error retrieving speaker views: {e}"

# ---------------------------------------------------------------------------
# 3. get_contrarian_opinions
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_contrarian_opinions(limit: Optional[int] = None) -> str:
    """
    Extracts high-conviction contrarian/consensus-defying macroeconomic views.
    If limit is specified, returns up to limit reports, otherwise returns all.
    """
    query = """
        SELECT r.video_id, r.speaker_name, r.speaker_role, r.source_channel,
               r.broadcast_date, r.time_box, r.core_thesis, r.verbatim_quote,
               r.conditional_catalysts, r.invalidation_risks,
               r.key_data_points, r.additional_quotes, r.price_targets, r.speaker_institution,
               r.expectation_gap, r.causal_chain, r.tracking_indicators, r.tactical_stance,
               q.bull_bear_score, q.conviction_score, q.contrarian_flag,
               q.sector_tilt, q.duration_call, q.macro_factor, q.view_time_horizon
        FROM reports r
        JOIN quant_signals q ON r.video_id = q.video_id
        WHERE q.contrarian_flag = 1
        ORDER BY r.broadcast_date DESC
    """
    params = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    try:
        rows = await query_db_async(query, params)
        if not rows:
            return "No contrarian opinions found in the database."
        return json.dumps(rows, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error retrieving contrarian opinions: {e}"

# ---------------------------------------------------------------------------
# 4. get_reports_by_timebox
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_reports_by_timebox(time_box: str) -> str:
    """
    Retrieves macro opinions targetting a specific period (e.g. '[[2026-H2]]', '[[2026]]').
    Use this to aggregate all expert opinions targeting a specific time window.
    """
    # Auto-wrap in double brackets if missing
    clean_tb = time_box.strip()
    if not clean_tb.startswith("[["):
        clean_tb = "[[" + clean_tb
    if not clean_tb.endswith("]]"):
        clean_tb = clean_tb + "]]"

    query = """
        SELECT r.video_id, r.speaker_name, r.speaker_role, r.source_channel, 
               r.broadcast_date, r.core_thesis,
               q.bull_bear_score, q.conviction_score
        FROM reports r
        LEFT JOIN quant_signals q ON r.video_id = q.video_id
        WHERE r.time_box = ?
        ORDER BY r.broadcast_date DESC
    """
    try:
        rows = await query_db_async(query, (clean_tb,))
        if not rows:
            return f"No reports found targeting timebox: '{clean_tb}'."
        return json.dumps(rows, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error retrieving reports for timebox '{time_box}': {e}"

# ---------------------------------------------------------------------------
# 5. read_obsidian_report
# ---------------------------------------------------------------------------
@mcp.tool()
async def read_obsidian_report(video_id: str, read_all: bool = True, max_chars: int = 1000000) -> str:
    """
    Reads the content of a specific Obsidian markdown report file in the vault by its Video ID.

    👑 [Ver 3.0] Default `max_chars` raised from 100k → 1,000,000 to unlock the full
    1M-context window of frontier reasoning models (Claude 3.5 Sonnet, DeepSeek V4 Pro).
    The downstream Stage 2 (meta/llama-3.1-70b-instruct via nvidia-api-proxy) already produced the compressed,
    high-signal markdown — there is no benefit to early-truncating it for a frontier
    model. Pass `read_all=False` to opt into the legacy truncation behaviour.

    Args:
        video_id: YouTube video ID (also matches the suffix of the .md filename).
        read_all: If True, return the full document regardless of size. If False,
                 truncate at `max_chars`.
        max_chars: Hard ceiling for the returned string. Default 1,000,000 (1 MB).
    """
    if not VAULT_PATH.exists():
        return "Obsidian vault directory not found."

    # 👑 경로 순회/glob 메타 방어 — video_id 는 11자 YouTube ID 패턴만 허용.
    # 이전엔 rglob(f"*{video_id}*") 에 "../" 또는 glob 메타가 그대로 전달되어
    # vault 외부 파일 탐색 가능했음.
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        return f"Invalid video_id: must be 11-char YouTube ID (got {video_id!r})."

    # Look for the markdown file containing the video_id recursively inside vault_path
    target_file = None
    try:
        # Loop through subdirectories to locate file
        for path in VAULT_PATH.rglob(f"*{video_id}*.md"):
            target_file = path
            break

        if not target_file or not target_file.exists():
            return f"No obsidian markdown file found for Video ID: {video_id}."

        async with aiofiles.open(target_file, mode='r', encoding='utf-8') as f:
            content = await f.read()

        if not read_all and len(content) > max_chars:
            truncated = content[:max_chars]
            return (
                f"{truncated}\n\n"
                f"... [TRUNCATED at {max_chars:,} chars of {len(content):,} total. "
                f"Re-call read_obsidian_report with read_all=True to read the full document.]"
            )
        return content
    except Exception as e:
        return f"Error reading Obsidian note for {video_id}: {e}"

# ---------------------------------------------------------------------------
# 6. get_adjacent_nodes
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_adjacent_nodes(node_value: str) -> str:
    """
    Queries the relational nodes table to find all co-occurring graph nodes linked to the input node.
    Performs a ultra-fast SQL JOIN to find nodes mentioned in the same context/video.
    Returns linked nodes sorted by relationship weight (frequency).
    """
    # Auto-wrap in double brackets if missing
    clean_val = node_value.strip()
    if not clean_val.startswith("[["):
        clean_val = "[[" + clean_val
    if not clean_val.endswith("]]"):
        clean_val = clean_val + "]]"

    query = """
        SELECT n2.node_value, n2.node_type, COUNT(DISTINCT n1.video_id) as weight
        FROM nodes n1
        JOIN nodes n2 ON n1.video_id = n2.video_id
        WHERE n1.node_value = ? AND n2.node_value != ?
        GROUP BY n2.node_value, n2.node_type
        ORDER BY weight DESC, n2.node_value ASC
    """
    try:
        rows = await query_db_async(query, (clean_val, clean_val))
        if not rows:
            return f"No adjacent relationships found for node: '{clean_val}'."
        return json.dumps(rows, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error traversing adjacent nodes: {e}"

# ---------------------------------------------------------------------------
# 7. run_macro_query (Read-Only Locking)
# ---------------------------------------------------------------------------
@mcp.tool()
async def run_macro_query(sql_query: str) -> str:
    """
    Executes a custom read-only SQL query on the database.
    Allows deep custom analysis (e.g. average conviction by channel, speaker counts).
    Protected by strict sqlite3 read-only URI constraint.
    """
    # 👑 허용목 기반 검증 — 첫 키워드가 SELECT/WITH 인 경우만 실행.
    # 이전 부분문자열 블랙리스트는 합법 SELECT(SELECT created_at / WHERE updated_at
    # 등 'create'/'update' 부분문자열 히트)를 차단하는 false positive 와
    # ATTACH/PRAGMA 우회를 동시에 야기. mode=ro 가 1차 쓰기 차단이므로 여기서는
    # 읽기 전용 첫 키워드만 허용.
    cleaned = re.sub(r"--.*?$|/\*.*?\*/", " ", sql_query, flags=re.DOTALL | re.MULTILINE).strip()
    first_kw = cleaned.split(None, 1)[0].lower() if cleaned else ""
    if first_kw not in ("select", "with"):
        return (
            f"Rejected: Custom query must start with SELECT or WITH "
            f"(got '{first_kw or 'empty'}'). Read-only queries only — "
            f"ATTACH/PRAGMA/INSERT/UPDATE/DELETE/etc are blocked."
        )

    # 👑 [2026-08-06 M2] 재귀 CTE DoS 차단 — WITH RECURSIVE 는 SQLite 가 종료
    # 기준을 보장할 수 없어 수 분간 CPU 를 소모(텔레그램 tool-calling 경로 노출,
    # 봇 응답 마비). 명시적 거부.
    if re.search(r"\bWITH\s+RECURSIVE\b", cleaned, re.IGNORECASE):
        return (
            f"Rejected: WITH RECURSIVE (recursive CTE) is blocked — unbounded "
            f"iteration can stall the bot. Use plain SELECT or non-recursive WITH."
        )

    # 결과 LIMIT 캡 — 대량 반환/과도 연산 방지. SELECT 에 LIMIT 없으면 200 캡.
    if first_kw == "select" and not re.search(r"\bLIMIT\b", cleaned, re.IGNORECASE):
        cleaned = cleaned.rstrip(";").rstrip() + " LIMIT 200"
        sql_query = cleaned

    try:
        rows = await query_db_async(sql_query)
        if not rows:
            return "Query executed successfully. Empty result set returned."
        return json.dumps(rows, indent=2, ensure_ascii=False)
    except sqlite3.OperationalError as oe:
        return f"Database Security Enforcement Error (Query was rejected or syntax invalid): {oe}"
    except Exception as e:
        return f"Execution failed: {e}"

# ---------------------------------------------------------------------------
# 8. get_pipeline_status
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_pipeline_status() -> str:
    """
    Returns statistics and status of the knowledge graph pipeline.
    Shows the total count of processed videos, speaker counts, and database summary statistics.
    """
    try:
        total_reports = await query_db_async("SELECT COUNT(*) as cnt FROM reports")
        total_nodes = await query_db_async("SELECT COUNT(*) as cnt FROM nodes")
        speakers = await query_db_async("SELECT COUNT(DISTINCT speaker_name) as cnt FROM reports")
        by_channel = await query_db_async("SELECT source_channel, COUNT(*) as cnt FROM reports GROUP BY source_channel ORDER BY cnt DESC")
        contrarians = await query_db_async("SELECT COUNT(*) as cnt FROM quant_signals WHERE contrarian_flag = 1")
        
        status = {
            "database_file": DB_PATH.name,
            "total_extracted_opinions": total_reports[0]["cnt"] if total_reports else 0,
            "total_graph_node_mappings": total_nodes[0]["cnt"] if total_nodes else 0,
            "unique_speakers_tracked": speakers[0]["cnt"] if speakers else 0,
            "high_conviction_contrarians": contrarians[0]["cnt"] if contrarians else 0,
            "source_channels_distribution": {row["source_channel"]: row["cnt"] for row in by_channel} if by_channel else {}
        }
        return json.dumps(status, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error retrieving pipeline status: {e}"

if __name__ == "__main__":
    # Start the fastmcp stdio transport server
    mcp.run()
