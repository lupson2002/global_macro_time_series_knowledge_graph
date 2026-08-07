# -*- coding: utf-8 -*-
"""
Ver 4.0 — Telegram Master Agent Orchestrator
=============================================
A long-running async service on the mini-PC (N100) that:

  1. Polls Telegram for incoming user messages (`TELEGRAM_BOT_TOKEN`)
  2. Acts as an MCP **Host**: connects to both
        - `src/mcp_server.py`     (read-only SQLite, 8 tools)
        - `src/lancedb_store.py` (semantic RAG, 2 tools)
     and collects every tool spec at startup.
  3. Calls Ollama Pro Cloud (`OLLAMA_PRO_BASE_URL` + `OLLAMA_PRO_API_KEY`)
     with the merged tool spec and the user's question.
  4. Loops on `tool_calls` returned by the model: dispatches each call to
     the local MCP tool, feeds the result back, until the model emits a
     final assistant message.
  5. Sends the final answer back to the user's Telegram chat as markdown.

The bot is *transport-agnostic* in two ways:
  • MCP servers can run as in-process imports (default) or as stdio subprocesses
    (toggle with `MCP_TRANSPORT=stdio`).
  • The Ollama client speaks OpenAI's chat completions spec, so the same
    code works against Ollama Pro (`https://ollama.com/v1`), DeepSeek,
    OpenAI, or any compatible endpoint.

Run:  `python src/telegram_bot.py`
      (env: TELEGRAM_BOT_TOKEN, OLLAMA_PRO_API_KEY, OLLAMA_PRO_BASE_URL)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any

import httpx
from src.config import settings

# Telegram (python-telegram-bot v22+)
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    from telegram.constants import ChatAction, ParseMode
except ImportError as e:
    print(f"[FATAL] python-telegram-bot is required: {e}\n"
          f"        Install: .venv/bin/pip install python-telegram-bot")
    sys.exit(1)

# Local in-process MCP servers (preferred for low latency)
from src.mcp_server import (
    get_recent_reports, get_speaker_views, get_contrarian_opinions,
    get_reports_by_timebox, read_obsidian_report, get_adjacent_nodes,
    run_macro_query, get_pipeline_status,
)
from src.lancedb_store import semantic_search_macro, get_vect_index_status

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = settings.telegram.bot_token
OLLAMA_PRO_API_KEY = settings.telegram.ollama_api_key
OLLAMA_PRO_BASE_URL = settings.telegram.ollama_base_url
OLLAMA_PRO_MODEL = settings.telegram.ollama_model
MCP_TRANSPORT = settings.telegram.mcp_transport

MAX_TOOL_ITER = settings.telegram.max_tool_iterations
OLLAMA_TIMEOUT_S = settings.telegram.timeout
TELEGRAM_MSG_LIMIT = settings.telegram.message_limit

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=settings.telegram.log_level,
)
logger = logging.getLogger("telegram_bot")

# ---------------------------------------------------------------------------
# Tool registry — in-process direct call (preferred, lowest latency)
# ---------------------------------------------------------------------------
TOOL_REGISTRY_INPROC: dict[str, Any] = {
    # mcp_server.py
    "get_recent_reports": get_recent_reports,
    "get_speaker_views": get_speaker_views,
    "get_contrarian_opinions": get_contrarian_opinions,
    "get_reports_by_timebox": get_reports_by_timebox,
    "read_obsidian_report": read_obsidian_report,
    "get_adjacent_nodes": get_adjacent_nodes,
    "run_macro_query": run_macro_query,
    "get_pipeline_status": get_pipeline_status,
    # lancedb_store.py
    "semantic_search_macro": semantic_search_macro,
    "get_vect_index_status": get_vect_index_status,
}


def tool_specs_openai() -> list[dict]:
    """Manually-authored OpenAI tool specs for our 10 tools.
    We don't have an `mcp` Python client that can introspect FastMCP
    tool schemas at runtime in this version, so we maintain specs
    here. The MCP servers remain the source of truth for *execution*;
    the spec is a hand-maintained contract kept in lockstep.
    """
    return [
        # ── mcp_server.py (8) ───────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "get_recent_reports",
                "description": "Retrieve the most recently ingested macro views with metadata, scores, and core theses.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Optional max number of reports."},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_speaker_views",
                "description": "Retrieve all views, conviction scores, and opinions for a specific speaker (LIKE search).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "speaker_name": {"type": "string"},
                    },
                    "required": ["speaker_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_contrarian_opinions",
                "description": "Extract high-conviction contrarian / consensus-defying macro views.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_reports_by_timebox",
                "description": "Retrieve all expert opinions targeting a specific period (e.g. '[[2026-H2]]').",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "time_box": {"type": "string"},
                    },
                    "required": ["time_box"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_obsidian_report",
                "description": "Read the full markdown content of an Obsidian note by YouTube video_id (up to 1 MB).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "read_all": {"type": "boolean", "default": True},
                        "max_chars": {"type": "integer", "default": 1000000},
                    },
                    "required": ["video_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_adjacent_nodes",
                "description": "Find all co-occurring graph nodes linked to the input node (e.g. ticker, theme, asset).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "node_value": {"type": "string"},
                    },
                    "required": ["node_value"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_macro_query",
                "description": "Execute a read-only SQL query against the macro knowledge DB (no INSERT/UPDATE/DELETE).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql_query": {"type": "string"},
                    },
                    "required": ["sql_query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_pipeline_status",
                "description": "Pipeline statistics: total reports, nodes, speakers, contrarian count, channel distribution.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        # ── lancedb_store.py (2) ───────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "semantic_search_macro",
                "description": "Semantic RAG: embed the query, find top-k most similar macro theses, return full reports.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query_text": {"type": "string"},
                        "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    },
                    "required": ["query_text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_vect_index_status",
                "description": "LanceDB index diagnostics (vector count, dimension and embedding backend).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------
async def dispatch_tool(name: str, arguments: dict | str) -> str:
    """Run a tool by name. Returns a string payload (the MCP tools already
    return JSON-encoded strings, so we don't double-encode).
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as je:
            # 👑 [A22] silent 폴백 경고화 — 빈 dict 로 도구가 기본값 실행되면
            # 잘못된 결과를 조용히 반환하므로 가시화.
            logger.warning("tool arg JSON parse failed for %s: %s — using empty dict", name, je)
            arguments = {}

    fn = TOOL_REGISTRY_INPROC.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)

    try:
        # Most tools are coroutines (async). Some are sync helpers — they
        # return strings directly. Normalize both into a string.
        if asyncio.iscoroutinefunction(fn):
            result = await fn(**arguments) if arguments else await fn()
        else:
            # Sync path: not used in this registry, but support it for safety
            result = fn(**arguments) if arguments else fn()
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return result
    except TypeError as e:
        # 👑 [A22] 인자 불일치 — no-arg 재시도는 잘못된 결과를 정상처럼 반환하므로
        # 제거하고 명시적 에러 반환.
        logger.warning("Tool %s argument mismatch: %s", name, e)
        return json.dumps({"error": f"Tool {name} argument mismatch: {e}"}, ensure_ascii=False)
    except Exception as e:
        logger.exception("Tool %s dispatch error", name)
        return json.dumps({"error": f"Tool {name} failed: {e}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Ollama Pro chat-completions with tool-calling loop
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are the chief macro research analyst of a top-tier global macro hedge fund. "
    "You have access to a private knowledge graph of expert opinions (Ray Dalio, "
    "Stanley Druckenmiller, Real Vision, Bloomberg, CNBC analysts, etc.) and a "
    "semantic vector index. When the user asks a question, you MUST first decide "
    "which tool(s) to call to gather evidence. Never fabricate speaker names or "
    "data. If the tools return no relevant results, say so explicitly. Format your "
    "final answer in clean markdown with [[ ]] backlinks for entity names."
)


async def call_ollama_with_tools(user_message: str) -> str:
    """One full tool-calling round-trip: returns the final assistant text."""
    if not OLLAMA_PRO_API_KEY:
        return "⚠️ `OLLAMA_PRO_API_KEY` is not set in `.env`. Cannot reach the cloud reasoner."

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    tools = tool_specs_openai()
    headers = {
        "Authorization": f"Bearer {OLLAMA_PRO_API_KEY}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(OLLAMA_TIMEOUT_S, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for iteration in range(MAX_TOOL_ITER):
            payload = {
                "model": OLLAMA_PRO_MODEL,
                "messages": messages,
                "tools": tools,
                "temperature": 0.2,
                "stream": False,
            }
            try:
                resp = await client.post(
                    f"{OLLAMA_PRO_BASE_URL.rstrip('/')}/chat/completions",
                    json=payload, headers=headers,
                )
            except httpx.HTTPError as e:
                logger.error("Ollama HTTP error: %s", e)
                return f"⚠️ Network error reaching Ollama Pro: `{e}`"

            if resp.status_code != 200:
                body = resp.text[:500]
                logger.error("Ollama non-200 (%s): %s", resp.status_code, body)
                return f"⚠️ Ollama Pro returned HTTP {resp.status_code}:\n```\n{body}\n```"

            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            finish = choice.get("finish_reason")
            tool_calls = msg.get("tool_calls") or []

            # If no tool calls → final answer
            if not tool_calls:
                content = msg.get("content", "")
                if not content and finish == "length":
                    content = "_(response truncated — model hit token limit)_"
                return content or "_(model returned no content)_"

            # Otherwise: feed the assistant message back + dispatch each tool
            messages.append(msg)  # preserve tool_calls field

            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                logger.info("🔧 tool_call: %s(%s)", name, raw_args[:200])

                tool_result_text = await dispatch_tool(name, raw_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": name,
                    "content": tool_result_text,
                })

        return "⚠️ Tool-calling loop exceeded `MAX_TOOL_ITER` without a final answer."


def _chunk_lines(text: str, max_len: int) -> list:
    """라인 경계 분할 — 코드펜스/굵게/링크 중간 절단 방지. 단일 라인이
    max_len 초과 시 강제 분할(이전 문자 단위 절단은 마크다운 파싱 실패 유발)."""
    if len(text) <= max_len:
        return [text] if text else []
    chunks: list = []
    current = ""
    for line in text.split("\n"):
        test = (current + "\n" + line) if current else line
        if len(test) <= max_len:
            current = test
        else:
            if current:
                chunks.append(current)
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            current = line
    if current:
        chunks.append(current)
    return chunks


async def send_chunked(update: Update, text: str):
    """Send a possibly-long message in chunks that respect Telegram's limit."""
    if not text:
        return
    # 👑 [A23] 라인 경계 분할(마크다운 중간 절단 방지) 후 MARKDOWN 시도,
    # 실패 시 plain 폴백.
    for chunk in _chunk_lines(text, TELEGRAM_MSG_LIMIT):
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            # Retry without markdown if Telegram rejects the formatting
            await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 글로벌 매크로 지식 그래프 에이전트 (Ver 4.0)\n\n"
        "매크로/금융 질문을 자유롭게 보내주세요. 로컬 LanceDB 인덱스에서 "
        "관련 구루 의견을 즉시 검색한 뒤 Ollama Pro가 종합 답변을 작성합니다.\n\n"
        "예: `AI capex 2026에 대한 전문가 의견은?`\n"
        "예: `현재 contrarian view 3개만 보여줘`\n"
        "예: `[[2026-H2]] 기간의 모든 thesis 요약`"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick diagnostic: pipeline + vector index status."""
    pipeline = await dispatch_tool("get_pipeline_status", {})
    vector = await dispatch_tool("get_vect_index_status", {})
    await update.message.reply_text(
        f"*Pipeline Status*\n```\n{pipeline}\n```\n\n*LanceDB Index*\n```\n{vector}\n```",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    chat = update.effective_chat
    logger.info("📨 user=%s message=%r", chat.id if chat else "?", user_text[:200])

    # Typing indicator
    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    start = time.time()
    try:
        answer = await call_ollama_with_tools(user_text)
    except Exception as e:
        logger.exception("Top-level orchestrator error")
        answer = f"❌ Internal error: `{e}`"

    elapsed = time.time() - start
    header = f"_🤖 Ollama Pro · {OLLAMA_PRO_MODEL} · {elapsed:.1f}s_\n\n"
    await send_chunked(update, header + (answer or "_(empty response)_"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ `TELEGRAM_BOT_TOKEN` missing in `.env`.")
        sys.exit(1)
    if not OLLAMA_PRO_API_KEY:
        print("⚠️  `OLLAMA_PRO_API_KEY` missing — bot will start but cloud calls will fail.")

    print("=" * 60)
    print("🤖 Ver 4.0 Telegram Master Agent")
    print(f"   Telegram token:    {TELEGRAM_BOT_TOKEN[:8]}…")
    print(f"   Ollama Pro model:  {OLLAMA_PRO_MODEL}")
    print(f"   Ollama Pro URL:    {OLLAMA_PRO_BASE_URL}")
    print(f"   MCP transport:     {MCP_TRANSPORT}")
    print(f"   Tools registered:  {len(TOOL_REGISTRY_INPROC)}")
    print("=" * 60)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("✅ Polling Telegram for messages…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
