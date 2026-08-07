"""RAG + LLM 인사이트 — semantic_search_macro(LanceDB) + NIM LLM 정성 분석.

자연어 질의("현재 가장 분기된 자산", "contrarian vs 컨센서스") → 관련 뷰+점수+노드 → LLM 정성 분석.
lancedb_store.search_hybrid + NIM 패턴 재사용 (구 turbovec_server 제거).
"""
from __future__ import annotations

import json

from src.config import settings

NIM_BASE_URL = settings.llm.nim_base_url
NIM_API_KEY = settings.llm.nim_api_key
# 2026-08-06 사용자 결정: insight/주간 추론도 flash 로 통일(pro→flash 다운그레이드 승인).
# INSIGHT_MODEL > TIER3_MODEL > 기본값 순. .env 로 오버라이드 가능.
INSIGHT_MODEL = settings.llm.insight_model
TIER2_TIMEOUT = settings.llm.tier2_timeout


def _parse_json_list(raw):
    """👑 [Ver 4.4] reports JSON 컬럼 안전 파싱 → list. NULL/빈/파손 → []."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _enriched_view_line(v: dict, thesis_limit: int = 200) -> str:
    """👑 [Ver 4.4] 구루 발언 한 줄 — quote·catalysts·risks·tactical·수치·가격목표 포함."""
    parts = [
        f"- 화자={v.get('speaker_name','?')}",
        f"채널={v.get('source_channel','?')}",
        f"bull_bear={v.get('bull_bear_score','?')}/10",
        f"contrarian={v.get('contrarian_flag',0)}",
    ]
    inst = v.get("speaker_institution")
    if inst:
        parts.append(f"소속={inst}")
    horizon = v.get("view_time_horizon")
    if horizon:
        parts.append(f"호리즌={horizon}")
    tac = []
    for k in ("sector_tilt", "duration_call", "macro_factor"):
        if v.get(k):
            tac.append(f"{k}={v.get(k)}")
    if tac:
        parts.append("tactical=" + ",".join(tac))
    parts.append(f"thesis=\"{v.get('core_thesis','')[:thesis_limit]}\"")
    quote = v.get("verbatim_quote")
    if quote:
        parts.append(f"quote=\"{quote}\"")
    cats = _parse_json_list(v.get("conditional_catalysts"))
    risks = _parse_json_list(v.get("invalidation_risks"))
    kdp = _parse_json_list(v.get("key_data_points"))
    pt = _parse_json_list(v.get("price_targets"))
    if cats:
        parts.append("촉매=" + "; ".join(cats))
    if risks:
        parts.append("리스크=" + "; ".join(risks))
    if kdp:
        parts.append("수치=" + "; ".join([f"{d.get('indicator','')}={d.get('value','')}{d.get('unit','')}" for d in kdp if isinstance(d, dict)]))
    if pt:
        parts.append("목표=" + "; ".join([f"{p.get('ticker','')} {p.get('direction','')}->{p.get('target','')}" for p in pt if isinstance(p, dict)]))
    # 👑 [Ver 4.7] 4대 내러티브 필드 노출 (마켓 내러티브/CIO RAG용)
    gap = v.get("expectation_gap")
    if gap:
        parts.append(f"기대격차={gap}")
    causal = _parse_json_list(v.get("causal_chain"))
    if causal:
        parts.append("인과체인=" + " -> ".join(str(c) for c in causal))
    trk = _parse_json_list(v.get("tracking_indicators"))
    if trk:
        parts.append("모니터링=" + "; ".join([f"{t.get('metric','')}@{t.get('threshold','')}" for t in trk if isinstance(t, dict)]))
    tac_st = _parse_json_list(v.get("tactical_stance"))
    if tac_st:
        parts.append("포지션=" + "; ".join([f"{t.get('asset','')}={t.get('stance','')}" for t in tac_st if isinstance(t, dict)]))
    return " | ".join(parts)


def search_macro_sync(query: str, top_k: int = 10, use_timebox: bool = True) -> list[dict]:
    """LanceDB 하이브리드 검색 동기 래퍼 (구 turbovec 대체).
    use_timebox 시 time_box 유효(만료 제외) + 빈값은 broadcast_date 90일 post-filter.
    """
    from src import lancedb_store
    from .timebox import is_valid_time_box
    from datetime import date as _date

    rows = lancedb_store.search_hybrid(query, limit=top_k * 3 if use_timebox else top_k)
    vids = [r["video_id"] for r in rows]
    views = lancedb_store.hydrate_views(vids)
    if use_timebox:
        today = _date.today()
        out = []
        for v in views:
            bd = v.get("broadcast_date")
            bd_date = None
            if bd:
                try:
                    bd_date = _date.fromisoformat(str(bd)[:10])
                except Exception:
                    bd_date = None
            if is_valid_time_box(v.get("time_box", ""), today=today, broadcast_date=bd_date):
                out.append(v)
        views = out
    return views[:top_k]


def _call_llm(system: str, user: str) -> str:
    """👑 [Ollama 전환] cloud_client (Ollama Cloud 우선, NIM 폴백)."""
    from src import cloud_client
    return cloud_client.chat_completion(
        system=system, user=user, max_tokens=2048, temperature=0.3, nim_model=INSIGHT_MODEL,
    )


SYSTEM_PROMPT = """당신은 글로벌 매크로 퀀트 큐레이터. 제공된 구루들의 발언(views)과 정량 신호(bull_bear, conviction, contrarian)를 종합해 **한국어 인사이트**를 도출.
- 정량 수치(수익률/MDD) 추정 금지. 관측된 의견 분포·컨센서스·분기·contrarian 비대칭에 집중.
- 불릿 + inline bold. 3-5줄 핵심 인사이트 + 1-2개 관찰점.
"""


def generate_insight(query: str, top_k: int = 10, use_timebox: bool = True) -> str:
    """query 로 RAG 검색 후 LLM 정성 분석. use_timebox 시 만료 time_box 제외."""
    views = search_macro_sync(query, top_k=top_k, use_timebox=use_timebox)
    if not views:
        return f"(검색 결과 없음: {query})"

    context_lines = []
    for v in views[:top_k]:
        context_lines.append(_enriched_view_line(v))
    context = "\n".join(context_lines)

    user = f"질문/주제: {query}\n\n관련 구루 발언({len(views)}건):\n{context}\n\n이 주제의 컨센서스·분기·contrarian 관점을 한국어 인사이트로 정리."
    return _call_llm(SYSTEM_PROMPT, user)


def build_insights(queries: list[str] | None = None, use_timebox: bool = True) -> dict[str, str]:
    """여러 자연어 질의에 대한 인사이트. 기본 질의 세트. use_timebox 시 만료 제외."""
    if queries is None:
        queries = [
            "AI Infrastructure 투자 컨센서스와 리스크",
            "가장 의견이 분기된 자산군과 contrarian 관점",
            "현재 주도 매크로 테마와 약자 테마",
        ]
    return {q: generate_insight(q, use_timebox=use_timebox) for q in queries}


KEY_CONCLUSIONS_PROMPT = """당신은 글로벌 매크로 퀀트 전략가. 제공된 자산군별 컨센서스 통계와 **실제 구루 발언**을 종합해 **한국어 핵심 투자 결론**을 도출.

절대 규칙 (위반 시 보고서 무효):
1. **구루 이름 필수**: 각 근거는 반드시 데이터에 있는 실제 구루 이름(speaker_name)으로 시작. "데이터 내 구루 명시 없음"/"명시되지 않음" 금지. Unknown/Reporter 도 그대로 표기.
2. **실제 발언 인용만**: 구루의 실제 core_thesis 발언을 한국어로 요약해 근거로 씀. 가능하면 제공된 verbatim_quote/quote 를 우선 인용. **가상 의견/창작/환각 금지**. 데이터에 없는 발언은 쓰지 말 것.
3. 정량 수치(수익률/MDD) 추정 금지. 단, 데이터에 명시된 수치(key_data_points)와 가격 목표(price_targets)는 있는 그대로 인용 가능.
4. 한 자산당 구루 근거 2-3개 불릿.
5. 👑 [Ver 4.4] '투자 검토/회피 검토' 근거에는 해당 구루의 촉매(conditional_catalysts)·무효화 리스크(invalidation_risks)가 있으면 함께 표시. tactical signals(sector_tilt/duration_call/view_time_horizon)도 근거로 활용.

반드시 아래 3개 섹션을 마크다운으로 작성:

### 🟢 투자 검토 자산군 (컨센서스 강세)
상위 bull 자산군 1-2개. 각 자산마다 그 자산을 긍정한 **실제 구루 이름과 발언 요약**을 불릿으로:
- **자산**: ...
  - **구루 근거**: 화자명(채널) — 실제 발언 요약 한 줄 (+ 촉매/리스크가 있으면 한 줄)
  - **구루 근거**: 화자명(채널) — 실제 발언 요약 한 줄

### 🔴 회피 검토 자산군 (컨센서스 약세/과열)
bear 경사 자산군 1-2개. 같은 형식:
- **자산**: ...
  - **구루 근거**: 화자명(채널) — 실제 발언 요약 (+ 무효화 리스크)

### 💎 가장 유니크한 의견 (contrarian/독창)
contrarian_flag=1 인 실제 구루 발언 중 가장 비대칭/독창적 1-2개. **반드시 실제 발언**:
- **구루**: 화자명(채널) — **의견**: 실제 core_thesis 요약 — **근거**: 발언 핵심 한 줄
"""


def _views_block(views: list, label: str) -> list:
    """구루 발언 블록 포맷 — 👑 [Ver 4.4] 인용·촉매·리스크·tactical·수치 포함."""
    lines = [f"\n[{label}]"]
    for v in views:
        lines.append(_enriched_view_line(v))
    return lines


def generate_key_conclusions(matrices: dict, use_timebox: bool = True) -> str:
    """크로스 매트릭스 + RAG 검색(강세/약세/contrarian) → LLM 핵심결론(실제 구루 발언 기반)."""
    import pandas as pd

    asset = matrices.get("asset")
    context_lines = ["[자산군별 컨센서스 통계]"]
    if asset is not None and not asset.empty:
        for _, r in asset.head(10).iterrows():
            context_lines.append(
                f"- {r['asset_class']}: 평균심리 {r['avg_bull_bear']:.1f}/10, "
                f"stddev {r['stddev_bull_bear']:.2f}, contrarian {r['contrarian_pct']:.0f}%, n={int(r['n'])}"
            )
        bear = asset.sort_values("avg_bull_bear").head(5)
        context_lines.append("[약세 자산군]")
        for _, r in bear.iterrows():
            context_lines.append(f"- {r['asset_class']}: 평균심리 {r['avg_bull_bear']:.1f}/10")

    bull_asset = asset.iloc[0]["asset_class"] if asset is not None and not asset.empty else "equities"
    bear_asset = asset.sort_values("avg_bull_bear").iloc[0]["asset_class"] if asset is not None and not asset.empty else "bonds"
    # 강세 자산 구루 발언 (bull_bear 높은 실제 발언)
    views_bull = search_macro_sync(f"{bull_asset} bullish investment thesis", top_k=8, use_timebox=use_timebox)
    views_bull = [v for v in views_bull if (v.get("bull_bear_score") or 0) >= 6]
    context_lines += _views_block(views_bull, f"{bull_asset} 강세 구루 발언 (실제)")

    views_bear = search_macro_sync(f"{bear_asset} bearish risk", top_k=8, use_timebox=use_timebox)
    views_bear = [v for v in views_bear if (v.get("bull_bear_score") or 9) <= 5]
    context_lines += _views_block(views_bear, f"{bear_asset} 약세 구루 발언 (실제)")

    # contrarian 실제 발언 (contrarian_flag=1) — 유니크 의견용
    contra = asset.sort_values("contrarian_pct", ascending=False).head(3) if asset is not None and not asset.empty else pd.DataFrame()
    if not contra.empty:
        contra_asset = contra.iloc[0]["asset_class"]
        views_contra = search_macro_sync(f"{contra_asset} contrarian view", top_k=8, use_timebox=use_timebox)
        views_contra = [v for v in views_contra if v.get("contrarian_flag") == 1]
        context_lines += _views_block(views_contra, f"contrarian 실제 발언 (유니크 후보, {contra_asset})")

    user = (
        "\n".join(context_lines)
        + "\n\n위 데이터에서 실제 구루 이름과 실제 발언만 인용해 3개 섹션(투자 검토/회피 검토/유니크 의견) 핵심결론을 작성. "
        "구루 이름이 데이터에 없으면 그 항목은 제외. 가상 발언/창작 절대 금지."
    )
    return _call_llm(KEY_CONCLUSIONS_PROMPT, user)


if __name__ == "__main__":
    r = generate_insight("AI Infrastructure 컨센서스")
    print(r)
