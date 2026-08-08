#!/usr/bin/env python3
"""Decision-first weekly report combining CIO, narrative, and insight layers."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.insights.market_narrative import collect_rag_context  # noqa: E402
from src.config import settings  # noqa: E402
from src.derived_llm import DerivedLLMRequest, complete_derived  # noqa: E402
from src.report_artifacts import weekly_artifacts, write_report_artifacts  # noqa: E402
from src.report_generator import send_email_report  # noqa: E402
from src.report_rendering import render_frontmatter  # noqa: E402
from src.run_events import ReportRunJournal  # noqa: E402
from src.weekly_signals import build_signal_snapshot, save_snapshot  # noqa: E402

DB_PATH = settings.storage.sqlite_path
VAULT_PATH = settings.storage.obsidian_vault
BASELINE_PATH = PROJECT_ROOT / "data" / "weekly_signal_baseline.json"

WEEKLY_SYSTEM_PROMPT = """당신은 투자위원회용 주간 리서치 편집자입니다.
입력에는 결정론적으로 계산된 변화 신호와 실제 발언 검색 결과만 있습니다.

절대 규칙:
- 입력에 없는 수치, 임계값, 가격목표, 확률, 인용을 만들지 마세요.
- 자산배분을 임의의 퍼센트로 제안하지 말고 확대/유지/축소/헤지의 방향만 제시하세요.
- 컨센서스와 사실을 구분하고, 의견 분열을 곧바로 투자기회라고 부르지 마세요.
- 각 판단 뒤에 [통계] 또는 [발언: 화자/채널] 근거를 붙이세요.
- 근거가 부족하면 '근거 부족'이라고 쓰세요.
- 짧고 직접적인 한국어로 작성하세요.

백링크 규칙:
- 화자명, 자산군, 티커, 거시 테마명은 [[ ]] 백링크로 감싸세요 (예: [[미국 대형주]], [[제롬 파월]], [[인플레이션]]).
- 일반 명사, 형용사, 동사, 조사는 백링크로 감싸지 마세요 (예: [[주식]], [[상승]], [[보고서]] 금지).
- 같은 표현이 문단 내 반복될 때는 첫 번째만 백링크하고 나머지는 일반 텍스트로 두세요.

정확히 다음 구조로 작성하세요:
# 주간 투자정보 통합 보고서 ({date})
> **한 줄 결론:** 한 문장

## 1. Executive Decision Brief
- **지난주 대비 핵심 변화:** 정확히 3개
- **확대 검토:** 방향과 근거
- **축소·헤지 검토:** 방향과 근거
- **이번 주 관찰:** 실제 입력에 있는 조건만

## 2. Regime & Narrative Shift
- 강화 중, 약화 중, 신규 등장 내러티브를 구분
- 지배적 내러티브의 반대 증거를 반드시 포함

## 3. Scenario & Portfolio Implications
| 시나리오 | 시점 | 입력에 존재하는 발동 조건 | 수혜 | 피해 | 대응 방향 | 무효화 |
|---|---|---|---|---|---|---|
시점 컬럼은 이번 주 / 1-3개월 / 3-6개월 중 하나만 선택.
기본/상방/하방 시나리오 각 1개. 임의 확률 금지.

## 4. 전략적 틸트
| 자산군 | 방향 | 강도 | 근거 |
|---|---|---|---|
방향: 확대 / 유지 / 축소 / 헤지 중 하나. 퍼센트 금지.
강도: 경미 / 보통 / 강력 중 하나.
근거: [통계] 또는 [발언: 화자/채널] 태그 필수.
입력 신호 보드의 자산군만 사용. 근거 부족 시 '근거 부족' 명시.

## 5. Disagreement & Asymmetry
- 가장 중요한 의견 차이와 양측 근거
- 어느 쪽이 맞는지 단정하지 말고 다음 확인 이벤트 제시
"""


def _change(value) -> str:
    if value is None:
        return "비교 불가"
    return f"{value:+d}p"


def render_signal_board(snapshot: dict) -> str:
    lines = [
        "## 6. Cross-Asset Signal Board",
        "",
        "> 채널별·화자별 반복을 먼저 평균한 뒤 채널을 동일 가중했습니다. 전주 변화가 핵심이며 수치는 수익률 예측이 아닙니다.",
        "",
        "| 자산 | 스탠스 | 전주 변화 | 합의도 | 분산 | 역설비 | 독립 화자 | 채널 | 테일리스크 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rows = 0
    for item in snapshot.get("assets", [])[:15]:
        rows += 1
        lines.append(
            f"| [[{item['asset']}]] | {item['stance']}/100 | {_change(item.get('delta'))} "
            f"| {item['agreement']}% | {item['dispersion']} | {item['contrarian_pct']}% "
            f"| {item['speakers']} | {item['channels']} | {item['tail_risk_ratio'] * 100:.0f}% |"
        )
    if rows == 0:
        lines.append("| 데이터 부족 | - | - | - | - | - | - | - | - |")
    return "\n".join(lines)


def render_evidence_appendix(snapshot: dict, rag_context: str) -> str:
    overall = snapshot["overall"]
    lines = [
        "## 부록 A. 결정론적 신호와 근거",
        "",
        f"- 분석 기간: {snapshot['windows']['current'][0]} ~ {snapshot['windows']['current'][1]}",
        f"- 비교 기간: {snapshot['windows']['previous'][0]} ~ {snapshot['windows']['previous'][1]}",
        f"- 전체 스탠스: {overall['stance']}/100 ({_change(overall.get('delta'))})",
        f"- 합의도: {overall['agreement']}% · 분산: {overall['dispersion']} · 역설비: {overall['contrarian_pct']}%",
        f"- 독립 화자 {overall['speakers']} · 채널 {overall['channels']} · 채널 기준 테일리스크: {overall['tail_risk_ratio'] * 100:.0f}%",
        "",
        "### 내러티브 변화 속도",
        "",
        "| 테마 | 최근 7일 | 이전 23일 | 일평균 속도비 | 신규 |",
        "|---|---:|---:|---:|:---:|",
    ]
    for item in snapshot.get("narrative_velocity", []):
        velocity = "신규" if item["velocity"] is None else f"{item['velocity']:.2f}x"
        lines.append(
            f"| [[{item['node']}]] | {item['count_7d']} | {item['count_prior_23d']} "
            f"| {velocity} | {'예' if item['new'] else '아니오'} |"
        )
    lines.extend([
        "", "### 실제 발언 검색 근거", "",
        "```text", rag_context.strip(), "```", "",
        "### 방법론 한계", "",
        "- 이 지표는 수집 콘텐츠의 변화이며 시장가격의 기대를 직접 측정하지 않습니다.",
        "- 스탠스는 채널·화자 편중을 완화했지만 동일 이벤트의 완전한 독립성을 보장하지 않습니다.",
        "- 실제 가격·밸류에이션·상관관계가 연결되기 전까지 포트폴리오 비중은 방향성으로만 해석합니다.",
    ])
    return "\n".join(lines)


def synthesize_weekly(snapshot: dict, rag_context: str) -> str:
    compact = {
        "overall": snapshot["overall"],
        "assets": snapshot["assets"][:15],
        "narrative_velocity": snapshot["narrative_velocity"],
        "windows": snapshot["windows"],
    }
    result = complete_derived(DerivedLLMRequest(
        pipeline="weekly", system=WEEKLY_SYSTEM_PROMPT.format(date=snapshot["as_of"]),
        user=("[결정론적 변화 신호]\n" + json.dumps(compact, ensure_ascii=False, indent=2)
              + "\n\n[실제 발언 검색 결과]\n" + rag_context),
        max_tokens=6144, temperature=0.2, nim_model=settings.llm.insight_model,
        ollama_attempts=3,
    )).content.strip()
    if not result:
        raise RuntimeError("통합 주간 보고서 LLM 응답이 비어 있습니다")
    return result


def _frontmatter(today: str) -> str:
    kst = datetime.now(timezone.utc) + timedelta(hours=9)
    return render_frontmatter((
        ("date", today), ("type", "weekly_investment_intelligence"),
        ("model", settings.llm.insight_model), ("provider", "ollama_nim_fallback"),
        ("generated_at", kst.strftime("%Y-%m-%dT%H:%M:%S+09:00")),
        ("tags", "[macro, weekly, investment_intelligence]"),
    ))


def generate_weekly_report(*, no_llm: bool = False, no_send: bool = False) -> tuple[str, tuple[Path, Path]]:
    snapshot = build_signal_snapshot(DB_PATH)
    rag_context = collect_rag_context(use_timebox=True, top_k=4) if not no_llm else "LLM/RAG 생략"
    if no_llm:
        synthesis = (
            f"# 주간 투자정보 통합 보고서 ({snapshot['as_of']})\n\n"
            "> **한 줄 결론:** LLM 생략 모드 — 결정론적 변화 신호와 근거만 제공합니다."
        )
    else:
        synthesis = synthesize_weekly(snapshot, rag_context)
    body = "\n\n".join((synthesis, render_signal_board(snapshot), render_evidence_appendix(snapshot, rag_context))) + "\n"
    artifacts = weekly_artifacts(PROJECT_ROOT, VAULT_PATH, snapshot["as_of"], body, _frontmatter(snapshot["as_of"]) + body)
    paths = write_report_artifacts(artifacts)
    save_snapshot(BASELINE_PATH, snapshot)
    if not no_send:
        send_email_report(f"📈 주간 투자정보 통합 보고서 - {snapshot['as_of']}", body)
    return body, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified weekly investment intelligence report")
    parser.add_argument("--no-send", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--event-log", "--event_log", dest="event_log")
    args = parser.parse_args(argv)
    event_path = PROJECT_ROOT / args.event_log if args.event_log else None
    journal = ReportRunJournal.from_path(event_path, "weekly", warn=lambda message: print(f"[WARN] {message}"))
    journal.started()
    try:
        body, paths = generate_weekly_report(no_llm=args.no_llm, no_send=args.no_send)
        print(f"✓ 통합 주간 보고서 저장: {paths[0]} / {paths[1]} ({len(body):,} chars)")
    except Exception as exc:
        journal.finished(success=False, stage="report", error=exc)
        print(f"❌ 통합 주간 보고서 실패: {exc}")
        return 1
    journal.finished(success=True, stage="delivery")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
