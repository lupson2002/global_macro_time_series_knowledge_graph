# -*- coding: utf-8 -*-
"""
market_narrative.py — Market Narrative Search Engine Report 코어 엔진 (신규 독립 모듈)
=====================================================================================
6대 내러티브 쿼리(4대 진단 + 긍정/부정 시나리오)로 구루 실제 발언(verbatim_quote/core_thesis)을 RAG 검색하고,
SQLite 정량 통계(의견 분열도 stddev, bull_bear 평균)와 결합해
DeepSeek-v4-flash(NIM/Ollama, INSIGHT_MODEL — 2026-08-06 사용자 결정: pro→flash 통일)로 다음을 추론·추출한다:
  - 현재 시장을 지배하는 내러티브 (Dominant Narrative)
  - 시장 상승의 핵심 병목 (The Market's Bottleneck / Missing Catalyst)
  - 내러티브 성패를 가를 3x3 시나리오 (상승 호재 3 + 하락 파탄 3)

기존 코드 수정 없음 — `src/insights/rag_insights` 의 RAG/LLM 래퍼,
`src/insights/cross_matrix`, `src/insights/timebox` 를 재사용한다.

Usage (코어 엔진 단독):
    .venv/bin/python scripts/insights/market_narrative.py            # 마크다운 출력
    .venv/bin/python scripts/insights/market_narrative.py --no-llm   # DB/통계만
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── 프로젝트 루트를 sys.path 에 등록 (src.* import 용) ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings  # noqa: E402
from src.derived_llm import DerivedLLMRequest, complete_derived  # noqa: E402

from src.insights.cross_matrix import asset_consensus_matrix  # noqa: E402
from src.insights.rag_insights import (  # noqa: E402
    INSIGHT_MODEL,
    _enriched_view_line,
    search_macro_sync,
)
from src.insights.timebox import valid_time_box_values  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "macro_knowledge.db"

NIM_BASE_URL = settings.llm.nim_base_url
NIM_API_KEY = settings.llm.nim_api_key
NARRATIVE_MAX_TOKENS = settings.llm.narrative_max_tokens

# ── RAG 전용 6대 내러티브 쿼리 (4대 진단 + 긍정/부정 시나리오) ──
NARRATIVE_QUERIES: dict[str, str] = {
    "dominant_story": (
        "What is the primary narrative, driver, or macro story moving the financial "
        "markets right now? Growth, Liquidity, Inflation, Earnings Quality, or Rate Cuts?"
    ),
    "market_bottleneck": (
        "What specific missing condition, data point, or catalyst is the market thirsty "
        "for to achieve its next leg up?"
    ),
    "great_disagreement": (
        "What is the biggest conflict or debate among macro experts and gurus right now?"
    ),
    "narrative_risk": (
        "What unexpected event or data could invalidate the current dominant market narrative?"
    ),
    "bullish_catalysts": (
        "What specific positive economic data, earnings surprises, liquidity expansion, "
        "or policy changes would confirm and boost the current macro narrative?"
    ),
    "bearish_shocks": (
        "What specific negative risks, inflation reignition, earnings misses, margin "
        "squeezes, or policy failures would break and ruin the current macro narrative?"
    ),
}

# 기본 RAG Top-K (쿼리당)
DEFAULT_TOP_K = 5

# ── DeepSeek-v4-Pro 내러티브 추론 프롬프트 (시스템 메시지) ──
NARRATIVE_PROMPT = """You are a World-Class Hedge Fund Chief Investment Officer (CIO).
Analyze the retrieved macro guru transcripts, core theses, and quantitative signal splits.

Your primary mission is to identify the CURRENT MARKET NARRATIVE and answer:
"What is the single most critical thing the market is hungry/thirsty for to drive the next leg up?"
(e.g., Growth rate, Rate cut timing, Quality of Earnings/Margins, Fiscal liquidity, AI ROI proof, Inflation deceleration)

### REQUIRED ANALYTICAL FRAMEWORK (Format in Korean):

# 🎯 마켓 내러티브 & 핵심 병목 진단 보고서 ({today_date})

> **💡 핵심 요약 (CIO Headline):** [시장의 현재 심리와 핵심 병목 1줄 요약]

---

## 1. 🔍 현재 시장을 지배하는 내러티브 (Dominant Narrative)
- **현재 시장 테마 레짐:** [Liquidity-Driven / Earnings-Quality / Fed-Dependent / Macro-Fear / Goldilocks 중 택 1]
- **주요 내러티브 상세:** 구루들이 공통적으로 주도하고 있는 논리의 핵심 구조 설명 (인용구 및 채널명 명시).
- **관련 백링크 노드:** [[백링크1]], [[백링크2]], [[백링크3]]

---

## 2. 🚰 시장 상승의 핵심 병목 조건 (The Market's Bottleneck)
*시장이 다음 상방 파동(Next Leg Up)을 만들기 위해 결정적으로 해소해야 하는 단 하나의 핵심 병목과 근거:*
1. **[병목 요소 1 - 가장 결정적]:** 왜 성장이나 금리보다 이 요소가 지금 가장 중요한지 구루 발언에 기반하여 논리적 설명.
2. **[병목 요소 2 - 서브 촉매]:** 보조적 확인 필요 수치/지표.

---

## 3. ⚔️ 전문가 의견 분열 & 병목 지점 (Consensus vs. Disagreement)
- **가장 격렬한 논쟁 지점:** (예: AI CapEx ROI vs 이익 훼손, 금리 인하 속도 vs 재인플레이션)
- **강세론(Bulls) 주장의 핵:** [핵심 논리 및 대표 구루]
- **약세론(Bears) 경고의 핵:** [핵심 논리 및 대표 구루]

---

## 4. 🔄 내러티브 전환 시나리오 & 트리거 (Narrative Pivot Triggers)
- **상방 확정 트리거 (Bullish Trigger):** 핵심 병목이 해소될 때의 주도 자산군.
- **내러티브 파탄 리스크 (Failure Risk):** 현재 내러티브가 깨지는 지점과 헤지 전략.

---

## 5. 📊 내러티브 기반 3x3 시나리오 분석 (Scenario Matrix)

*시맨틱 Vector DB에 축적된 전문가들의 긍정/부정 근거를 바탕으로, 현재 내러티브의 성패를 가를 3가지 상승 호재 및 3가지 하락 파탄 시나리오 분석:*

### 🟢 [상승 호재] 내러티브가 성공하기 위한 3대 필수 조건 (Bullish Scenarios)
1. **[상승 촉매 1]:**
   - **발동 조건 & 수치:** (예: OPM 15% 이상 유지, CPI 2.5% 이하 안착 등 구루들이 제시한 정량 지표)
   - **수혜 자산/섹터:** (예: Big Tech, Quality Growth, Corporate Bonds)
   - **전문가 근거:** [구루 발언/채널 인용]
2. **[상승 촉매 2]:**
   - **발동 조건 & 상세:**
   - **수혜 자산/섹터:**
   - **전문가 근거:**
3. **[상승 촉매 3]:**
   - **발동 조건 & 상세:**
   - **수혜 자산/섹터:**
   - **전문가 근거:**

### 🔴 [하락 파탄] 내러티브가 붕괴되는 3대 핵심 리스크 (Bearish Scenarios)
1. **[파탄 리스크 1]:**
   - **붕괴 지점(Crack Point):** (예: CapEx 대비 Free Cash Flow 마이너스 전환, 유가 $90 돌파 등)
   - **타격 자산/섹터:** (예: High-Beta Tech, Commercial Real Estate)
   - **전문가 경고:** [구루 발언/채널 인용]
2. **[파탄 리스크 2]:**
   - **붕괴 지점:**
   - **타격 자산/섹터:**
   - **전문가 경고:**
3. **[파탄 리스크 3]:**
   - **붕괴 지점:**
   - **타격 자산/섹터:**
   - **전문가 경고:**

---
*Rules:*
- Write in concise, professional Korean (CIO Tone).
- Base ALL claims on the provided context with verbatim quotes and channel names.
- Do NOT invent data."""


# ---------------------------------------------------------------------------
# 1) SQLite 정량 통계 (의견 분열도 stddev + bull/bear 평균 + contrarian)
# ---------------------------------------------------------------------------
def _timebox_where(use_timebox: bool) -> tuple[str, list]:
    """time_box 유효기간 WHERE 절 (cross_matrix._timebox_clause 와 동일 규칙).
    - time_box 유효(미래/현재) OR (빈값 + broadcast_date 최근 90일)
    - use_timebox=False → 전체
    """
    if not use_timebox:
        return "1=1", []
    valid = valid_time_box_values()
    # 👑 [2026-08-06 L4] date('now') 는 UTC — broadcast_date(KST) 와 1일 시차 보정.
    empty = "(r.time_box IS NULL OR r.time_box = '') AND r.broadcast_date >= date('now','+9 hours','-90 days')"
    if not valid:
        return f"({empty})", []
    ph = ",".join("?" * len(valid))
    return f"(r.time_box IN ({ph}) OR {empty})", list(valid)


def collect_stats(use_timebox: bool = True) -> str:
    """SQLite 정량 통계 문단 — 전체 유효 뷰 심리/확신/contrarian + 자산군 분열도."""
    lines = ["[SQLite 정량 통계]"]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where, params = _timebox_where(use_timebox)
        row = conn.execute(
            f"""SELECT COUNT(*) AS n,
                       ROUND(AVG(q.bull_bear_score), 2)  AS avg_bb,
                       ROUND(AVG(q.conviction_score), 2) AS avg_conv,
                       ROUND(100.0 * SUM(q.contrarian_flag) / COUNT(*), 1) AS contra_pct
                FROM reports r
                JOIN quant_signals q ON r.video_id = q.video_id
                WHERE q.bull_bear_score IS NOT NULL AND {where}""",
            params,
        ).fetchone()
    finally:
        conn.close()

    if row and row["n"]:
        lines.append(
            f"- 유효 뷰 {row['n']}건 · 평균 bull_bear {row['avg_bb']}/10 · "
            f"평균 conviction {row['avg_conv']}/10 · contrarian {row['contra_pct']}%"
        )
    else:
        lines.append("- 유효 뷰 없음 (필터 재확인)")

    # 자산군별 컨센서스 (분열도 stddev 상위 = 의견 갈림)
    asset = asset_consensus_matrix(use_timebox=use_timebox)
    if asset is not None and not asset.empty:
        top_bull = asset.iloc[0]
        top_bear = asset.sort_values("avg_bull_bear").iloc[0]
        top_div = asset.sort_values("stddev_bull_bear", ascending=False).iloc[0]
        lines.append(
            f"- 최고 bull: **{top_bull['asset_class']}** {top_bull['avg_bull_bear']:.1f}/10 (n={int(top_bull['n'])})"
        )
        lines.append(
            f"- 최고 bear: **{top_bear['asset_class']}** {top_bear['avg_bull_bear']:.1f}/10 (n={int(top_bear['n'])})"
        )
        lines.append(
            f"- 의견 분열도 최대: **{top_div['asset_class']}** (stddev {top_div['stddev_bull_bear']:.2f})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2) RAG 4대 쿼리 검색 → 구루 실제 발언 컨텍스트
# ---------------------------------------------------------------------------
def _view_line_with_nodes(v: dict) -> str:
    """_enriched_view_line + 백링크 노드(macro_themes/asset_classes/tickers) 보강."""
    line = _enriched_view_line(v)
    nodes = []
    for k in ("macro_themes", "asset_classes", "tickers"):
        vals = v.get(k) or []
        nodes.extend(vals)
    if nodes:
        line += " | nodes=" + ", ".join(nodes)
    return line


def collect_rag_context(use_timebox: bool = True, top_k: int = DEFAULT_TOP_K) -> str:
    """4대 내러티브 쿼리 각각 Top K 검색 → 실제 발언 문단."""
    lines = ["[구루 실제 발언 (RAG 검색 결과)]"]
    for key, q in NARRATIVE_QUERIES.items():
        lines.append(f"\n■ 쿼리 [{key}]: {q}")
        try:
            views = search_macro_sync(q, top_k=top_k, use_timebox=use_timebox)
        except Exception as e:  # noqa: BLE001
            lines.append(f"(검색 실패: {e})")
            continue
        if not views:
            lines.append("(검색 결과 없음)")
            continue
        for v in views:
            lines.append(_view_line_with_nodes(v))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3) 내러티브 추론 (DeepSeek-v4-Pro) → 리포트 마크다운
# ---------------------------------------------------------------------------
def _call_llm_narrative(system: str, user: str, max_tokens: int = NARRATIVE_MAX_TOKENS) -> str:
    """Ollama Cloud 우선/NIM 폴백 호출 — 내러티브 보고서는 섹션 5(3x3)까지 분량이 커
    `rag_insights._call_llm`(2048) 대신 max_tokens를 상향하고 공통 재시도 정책을 사용.

    기존 코드 수정 없이 market_narrative.py 전용 래퍼로 독립 실행.
    """
    try:
        text = complete_derived(DerivedLLMRequest(
            pipeline="narrative", system=system, user=user, max_tokens=max_tokens,
            temperature=0.3, nim_model=INSIGHT_MODEL, ollama_attempts=3,
        )).content
        if not text.strip():
            raise RuntimeError(f"Ollama/NIM({INSIGHT_MODEL}) 빈 응답")
        return text
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"내러티브 추론 실패: {exc}") from exc


def generate_narrative_report(use_timebox: bool = True, top_k: int = DEFAULT_TOP_K, no_llm: bool = False) -> str:
    """RAG + 정량 통계 → 공용 LLM 라우터 → 내러티브 리포트 마크다운 반환."""
    today = datetime.now().strftime("%Y-%m-%d")

    stats = collect_stats(use_timebox=use_timebox)
    rag_ctx = collect_rag_context(use_timebox=use_timebox, top_k=top_k)

    # 👑 [주간] 4대 고급 분석 — Velocity 스파이크 TOP3 + 구루 임계값 참조표 (섹션2/5 반영용)
    weekly_ctx = ""
    try:
        from scripts.insights.weekly_analytics import narrative_velocity, threshold_reference_table
        vel = narrative_velocity(top_k=3)
        if vel:
            vel_lines = ["\n[주간 내러티브 속도 스파이크 TOP3]", ""]
            for x in vel:
                vel_lines.append(
                    f"- {x['node']}: 7일 {x['count_7d']}회 / 30일일평균 {x['daily_avg_30d']} → velocity {x['velocity']}"
                )
            weekly_ctx += "\n".join(vel_lines) + "\n"
        weekly_ctx += "\n" + threshold_reference_table() + "\n"
    except Exception as e:
        print(f"[WARN] 주간 분석 컨텍스트 실패: {e}")

    if no_llm:
        return (
            f"# 🎯 마켓 내러티브 진단 (LLM 스킵 — 원자료만)\n\n"
            f"- 생성: {datetime.now().strftime('%Y-%m-%d %H:%M KST')}\n"
            f"- 필터: {'time_box 유효 + 빈값 90일' if use_timebox else '전체 DB'}\n\n"
            f"{stats}\n\n{rag_ctx}\n\n{weekly_ctx}\n"
        )

    user = (
        f"Today's Date: {today}\n\n"
        f"[유효성 필터]: {'time_box 유효(미래/현재) + 빈값 broadcast_date 90일' if use_timebox else '전체 DB'}\n\n"
        f"{stats}\n\n"
        f"{rag_ctx}\n\n"
        f"{weekly_ctx}\n\n"
        "위 데이터만 근거로 내러티브 진단 리포트를 작성하세요. "
        "인용은 반드시 제공된 실제 발언(화자명/채널명 명시)에서만.\n"
        "제공된 [주간 내러티브 속도 스파이크] 와 [구루 임계값 컨센서스 참조표] 는 반드시 "
        "섹션 2(시장의 목마름/병목)와 섹션 5(3x3 시나리오)에 근거로 반영하세요."
    )

    print(f"🤖 LLM(우선 Ollama, NIM 폴백: {INSIGHT_MODEL}) 내러티브 추론 중...")
    content = _call_llm_narrative(NARRATIVE_PROMPT.format(today_date=today), user)
    content = (content or "").strip()
    if not content:
        raise RuntimeError("NIM 내러티브 추론 결과가 비어 있음")

    # 👑 [주간] 결정론 부록 — velocity 스파이크 + 구루 임계값 참조표
    # (LLM 출력과 무관하게 항상 포함되어 시나리오 근거로 활용 가능)
    if weekly_ctx.strip():
        appendix = "\n\n---\n\n## 📊 주간 분석 참조 (Weekly Analytics)\n" + weekly_ctx
        content = content.rstrip() + appendix
    return content


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Market Narrative Search Engine Report 코어 엔진")
    ap.add_argument("--expiry", choices=["timebox", "all"], default="timebox",
                    help="timebox=time_box 유효기간(기본) | all=전체 DB")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--no-llm", action="store_true", help="LLM 스킵 — 원자료만 출력")
    args = ap.parse_args()
    print(generate_narrative_report(
        use_timebox=args.expiry == "timebox", top_k=args.top_k, no_llm=args.no_llm,
    ))
