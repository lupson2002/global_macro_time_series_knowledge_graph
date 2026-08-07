#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""👑 [Ver 4.4] 블로그 원고 자동 생성기.

강화된 DB reports(verbatim_quote·additional_quotes·key_data_points·price_targets·
catalysts·risks·tactical·speaker_institution·view_time_horizon) + 최신 Daily 리포트
내러티브를 입력으로 **한국어 기사 원고**를 생성해 tistory_draft.md 에 저장.
publish_all_blogs.py 가 이 파일을 발행(수동 실행).

설계(환각 차단):
  - 인용·수치·가격목표·링크는 DB 원본에서 결정론적 렌더(증거 블록).
  - LLM은 증거 블록 + Daily 내러티브를 받아 산문(제목·리드·본문·결론)만 작성.
    "제공된 인용·수치만 사용, 직접 인용은 따옴표 그대로, 새 수치/인용 창작 금지" 지시.
  - --no-llm 시 결정론적 증거 블록만 출력(초안 골격).

사용:
  python scripts/generate_blog_draft.py --days 7 [--theme AI] [--top 8] [--no-llm]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))  # src.llm_router 등 src.* import 용
DB_PATH = PROJECT_DIR / "data" / "macro_knowledge.db"
DAILY_DIR = PROJECT_DIR / "obsidian_vault" / "Daily_Reports"
DRAFT_PATH = PROJECT_DIR / "tistory_draft.md"

NIM_BASE_URL = os.environ.get("NIM_BASE_URL", "http://localhost:8000")
NIM_API_KEY = os.environ.get("NIM_API_KEY", "proxy-rotates-keys")
# 👑 [2026-08-06 L3] 미사용 BLOG_MODEL(정의만, EOL qwen3-next 기본값) 제거.
BLOG_TIMEOUT = float(os.environ.get("BLOG_TIMEOUT", "180.0"))


def _parse_json_list(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def fetch_recent_reports(days: int, theme: str | None = None) -> list[dict]:
    """최근 N일(KST 보정) reports + quant_signals + nodes. theme 필터(노드 값 포함)."""
    if not DB_PATH.exists():
        print(f"[WARN] DB 없음: {DB_PATH}")
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = """
        SELECT r.*, q.bull_bear_score, q.conviction_score, q.contrarian_flag,
               q.sector_tilt, q.duration_call, q.macro_factor, q.view_time_horizon
        FROM reports r
        LEFT JOIN quant_signals q ON r.video_id = q.video_id
        WHERE r.broadcast_date >= date('now', '+9 hours', ?)
    """
    params: list = [f"-{days} days"]
    if theme:
        query += """ AND EXISTS (
            SELECT 1 FROM nodes n WHERE n.video_id = r.video_id
            AND n.node_value LIKE ?)"""
        params.append(f"%{theme}%")
    query += " ORDER BY q.conviction_score DESC NULLS LAST, r.broadcast_date DESC"
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        cur.execute("SELECT node_type, node_value FROM nodes WHERE video_id = ?", (r["video_id"],))
        r["nodes"] = [dict(n) for n in cur.fetchall()]
    conn.close()
    return rows


def latest_daily_narrative() -> str:
    """최신 Daily 리포트 MD 본문(섹션1-4)을 내러티브 컨텍스트로 읽기."""
    files = sorted(DAILY_DIR.glob("Daily_Macro_Synthesis_*.md"), reverse=True)
    if not files:
        return ""
    text = files[0].read_text(encoding="utf-8")
    # frontmatter 제거
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:].strip()
    return text


def build_evidence_block(reports: list[dict], top_k: int) -> str:
    """결정론적 증거 블록 — contrarian 우선 + conviction 내림차순 top_k."""
    def sort_key(r):
        contra = 1 if r.get("contrarian_flag") else 0
        conv = r.get("conviction_score") or 0
        return (contra, conv)

    seen = set()
    selected = []
    for r in sorted(reports, key=sort_key, reverse=True):
        vid = r.get("video_id")
        if vid in seen:
            continue
        if not (r.get("verbatim_quote") or "").strip():
            continue
        seen.add(vid)
        selected.append(r)
        if len(selected) >= top_k:
            break

    if not selected:
        return "*(인용 가능한 고신용도 발언 없음)*"

    lines = ["### 📌 증거 (원문 자동 추출 — 기사 인용 단위로 그대로 활용 가능)\n"]
    for r in selected:
        speaker = r.get("speaker_name") or r.get("video_id") or "Unknown"
        meta = [x for x in [r.get("speaker_role"), r.get("speaker_institution"), r.get("source_channel"), r.get("broadcast_date")] if x]
        horizon = r.get("view_time_horizon")
        if horizon:
            meta.append(f"호리즌 {horizon}")
        vid = r.get("video_id") or ""
        url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
        themes = [n["node_value"] for n in r.get("nodes", []) if n["node_type"] == "macro_theme"]
        tickers = [n["node_value"] for n in r.get("nodes", []) if n["node_type"] == "ticker"]
        tags = ", ".join([*themes, *tickers])

        lines.append(f"- **[[{speaker}]]**" + (f" ({' · '.join(meta)})" if meta else ""))
        lines.append(f"  > 「{(r.get('verbatim_quote') or '').strip()}」")
        for q in _parse_json_list(r.get("additional_quotes")):
            if q and q.strip():
                lines.append(f"  > 「{q.strip()}」")
        if r.get("core_thesis"):
            lines.append(f"  - 핵심 주장: {r.get('core_thesis')}")
        cats = _parse_json_list(r.get("conditional_catalysts"))
        risks = _parse_json_list(r.get("invalidation_risks"))
        if cats:
            lines.append(f"  - 촉매: {'; '.join(cats)}")
        if risks:
            lines.append(f"  - 무효화 리스크: {'; '.join(risks)}")
        kdp = _parse_json_list(r.get("key_data_points"))
        if kdp:
            lines.append("  - 핵심 수치: " + " · ".join(
                [f"{d.get('indicator','')}: {d.get('value','')}{d.get('unit','')} ({d.get('context','')})" for d in kdp if isinstance(d, dict)]))
        pt = _parse_json_list(r.get("price_targets"))
        if pt:
            lines.append("  - 가격 목표: " + " · ".join(
                [f"{p.get('ticker','')} {p.get('direction','')}→{p.get('target','')} ({p.get('horizon','')})" for p in pt if isinstance(p, dict)]))
        src = ""
        if url:
            src = f"  - 출처: [YouTube]({url})"
            if tags:
                src += f" · {tags}"
        elif tags:
            src = f"  - 테마/티커: {tags}"
        if src:
            lines.append(src)
    return "\n".join(lines)


BLOG_SYSTEM_PROMPT = """당신은 글로벌 매크로 투자 전문 기자이자 칼럼니스트. 제공된 'Daily 내러티브'와 '증거 블록(실제 구루 직접 인용·수치·가격목표·출처 링크)'을 바탕으로 **한국어 기사 원고**를 작성.

절대 규칙(위반 시 원고 무효):
1. 인용은 증거 블록의 「...」 직접 인용을 그대로 따옴표 안에 사용. 절대 편파·요약·창작 금지.
2. 수치·가격 목표·화자명·소속은 증거 블록에 있는 것만 사용. 새 수치/인용/인물 창작 금지.
3. 기사 구조: 첫 줄 `# 제목`(20자 내외, 훅이 있는 제목) → 리드 1문장 → 본문(테마별 2-4문단, 인용과 수치로 근거 제시) → 결론 1문단.
4. 출처는 본문 중 인용 옆에 [YouTube](url) 인라인 링크로 표시(증거 블록 url 사용).
5. Obsidian 백링크 [[ ]]는 제거하고 일반 텍스트로(블로그 발행용).
6. 표·불릿을 적절히 활용하되 기사 산문이 메인. 홍보성·과장 표현 금지.
7. 정량 수익률/MDD 추정 금지. 데이터에 명시된 수치·가격목표만 인용.
"""


def llm_article(narrative: str, evidence: str, theme: str | None) -> str:
    """멀티 프로바이더 라우터로 기사 산문 생성. 반환: '# 제목\\n본문'."""
    from src.llm_router import Llama70BRouter
    router = Llama70BRouter()
    theme_hint = f" 주제 필터: {theme}." if theme else ""
    user = (
        f"아래는 최근 매크로 Daily 내러티브와 구루 증거 블록이다.{theme_hint}\n"
        f"이것만을 근거로 한국어 기사 원고를 작성하라.\n\n"
        f"=== Daily 내러티브 ===\n{narrative[:6000]}\n\n"
        f"=== 증거 블록(직접 인용·수치·출처) ===\n{evidence}\n\n"
        f"기사 원고를 작성. 첫 줄은 반드시 `# 제목`."
    )
    print(f"🤖 블로그 원고 생성 via Llama70BRouter (Cerebras/Groq, NIM 폴백)...")
    out = router.generate(system=BLOG_SYSTEM_PROMPT, user=user, max_tokens=4096, temperature=0.3)
    out = (out or "").strip()
    if not out.startswith("#"):
        out = "# " + out  # 제목 보정
    # 👑 블로그 발행용 — Obsidian 백링크 [[X]] → X 로 strip(규칙 #5).
    import re as _re
    out = _re.sub(r"\[\[([^\]]+)\]\]", r"\1", out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="블로그 원고 자동 생성기 (Ver 4.4)")
    ap.add_argument("--days", type=int, default=7, help="최근 N일 데이터 (기본 7)")
    ap.add_argument("--theme", default=None, help="테마/티커 필터 (예: AI, NVDA)")
    ap.add_argument("--top", type=int, default=8, help="증거 블록 상위 발언 수 (기본 8)")
    ap.add_argument("--no-llm", action="store_true", help="결정론적 증거 블록만 출력(LLM 산문 스킵)")
    ap.add_argument("--out", default=str(DRAFT_PATH), help="출력 파일 경로")
    args = ap.parse_args(argv)

    reports = fetch_recent_reports(args.days, args.theme)
    print(f"📊 수집: {len(reports)}건 (최근 {args.days}일{', 테마=' + args.theme if args.theme else ''})")
    if not reports:
        print("[INFO] 데이터 없음 — 원고 생성 스킵.")
        return 1

    evidence = build_evidence_block(reports, args.top)
    narrative = latest_daily_narrative()
    if not narrative:
        print("[INFO] 최신 Daily 리포트 없음 — 증거 블록만으로 생성.")

    if args.no_llm:
        title = f"# {datetime.datetime.now().strftime('%Y-%m-%d')} 매크로 내부자 인사이트 (증거 블록)"
        article = title + "\n\n" + evidence
    else:
        try:
            article = llm_article(narrative, evidence, args.theme)
        except Exception as e:
            print(f"[WARN] LLM 호출 실패({e}) — 결정론적 증거 블록으로 폴백.")
            title = f"# {datetime.datetime.now().strftime('%Y-%m-%d')} 매크로 내부자 인사이트"
            article = title + "\n\n" + (narrative[:3000] + "\n\n" if narrative else "") + evidence

    out_path = Path(args.out)
    # 기존 초안 백업
    if out_path.exists():
        bak = out_path.with_suffix(".md.bak")
        out_path.rename(bak)
        print(f"📋 기존 초안 백업: {bak}")
    out_path.write_text(article + "\n", encoding="utf-8")
    print(f"✓ 블로그 원고 저장: {out_path} ({len(article)}자)")
    print(f"  발행: python {PROJECT_DIR / 'publish_all_blogs.py'} (수동)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
