# -*- coding: utf-8 -*-
"""
Grand Reasoner Orchestrator for Global Macro Time-Series Knowledge Graph
======================================================================
1. Aggregates data by calling MCP server tools locally.
2. Directs the compiled macro context to a high-capacity Frontier Cloud Reasoning model.
3. Uses the shared LLM route: Ollama Cloud first, then the NIM model selected by TIER3_MODEL.
4. Runs no local model inference on this machine; both generation paths are remote.
5. Formats and exports a comprehensive 'Global Macro Asset Allocation Strategy' report to Obsidian Vault.
"""

import json
import time
import asyncio
import datetime
from pathlib import Path
import logging
from src.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path Resolution & Importer
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VAULT_PATH = PROJECT_ROOT / "obsidian_vault"

# Import MCP tools directly to avoid network/process overhead
from src import mcp_server
# 👑 메일 발송 — report_generator 의 send_email_report 재사용(DRY, 동일 Gmail 설정).
from src.report_generator import send_email_report

# ---------------------------------------------------------------------------
# Configuration Variables (Default with Environment overrides)
# ---------------------------------------------------------------------------
# Tier 3 NIM fallback configuration. General generation uses cloud_client.
NIM_BASE_URL = settings.llm.nim_base_url
NIM_API_KEY = settings.llm.nim_api_key
# 2026-08-06 사용자 결정: 주간 보고서는 flash 로 통일(pro→flash 다운그레이드 승인). .env 로 오버라이드 가능.
TIER3_MODEL = settings.llm.tier3_model
# 👑 구 REASONER_MODEL env(claude-3-5-sonnet 등 Anthropic 모델명)는 NIM 통일로 무효 —
# back-compat 덮어쓰기 제거(.env 의 REASONER_MODEL 이 NIM 없는 모델 → 404 방지).

# ---------------------------------------------------------------------------
# Knowledge Aggregation Step (Consumes MCP tools internally)
# 👑 [Ver 3.0] Fully-open async aggregation — every record, no truncation.
# Parallel gather across all data dimensions; single text buffer output.
# ---------------------------------------------------------------------------
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


def _render_report_block(idx: int, r: dict, contrarian: bool = False) -> str:
    """👑 [Ver 4.4] 단일 리포트를 CIO 컨텍스트 블록으로 렌더.
    인용·촉매·리스크·tactical·가격목표·수치·시간지평 포함(이전엔 core_thesis+scores만).
    """
    cats = _parse_json_list(r.get("conditional_catalysts"))
    risks = _parse_json_list(r.get("invalidation_risks"))
    kdp = _parse_json_list(r.get("key_data_points"))
    aq = _parse_json_list(r.get("additional_quotes"))
    pt = _parse_json_list(r.get("price_targets"))
    tactical = []
    for k, v in (("sector_tilt", r.get("sector_tilt")), ("duration_call", r.get("duration_call")),
                 ("macro_factor", r.get("macro_factor")), ("view_time_horizon", r.get("view_time_horizon"))):
        if v:
            tactical.append(f"{k}={v}")
    inst = r.get("speaker_institution") or ""
    parts = [
        f"{idx}. Speaker: {r.get('speaker_name')} ({r.get('speaker_role')}"
        + (f", {inst}" if inst else "") + ")\n",
        f"   Source: {r.get('source_channel')} | Broadcast Date: {r.get('broadcast_date')}\n",
        f"   Target Period: {r.get('time_box')} | "
        f"Bull/Bear Score: {r.get('bull_bear_score')}/10 | "
        f"Conviction: {r.get('conviction_score')}/10"
        + (" | Contrarian" if contrarian else "") + "\n",
        f"   Core Thesis: {r.get('core_thesis')}\n",
    ]
    if r.get("verbatim_quote"):
        parts.append(f"   Verbatim Quote: \"{r.get('verbatim_quote')}\"\n")
    for q in aq:
        if q:
            parts.append(f"   Additional Quote: \"{q}\"\n")
    if tactical:
        parts.append(f"   Tactical: {', '.join(tactical)}\n")
    if cats:
        parts.append(f"   Catalysts: {'; '.join(cats)}\n")
    if risks:
        parts.append(f"   Invalidation Risks: {'; '.join(risks)}\n")
    if kdp:
        parts.append("   Key Data Points: " + "; ".join(
            [f"{d.get('indicator','')}={d.get('value','')}{d.get('unit','')} ({d.get('context','')})" for d in kdp if isinstance(d, dict)]) + "\n")
    if pt:
        parts.append("   Price Targets: " + "; ".join(
            [f"{p.get('ticker','')} {p.get('direction','')}->{p.get('target','')} ({p.get('horizon','')})" for p in pt if isinstance(p, dict)]) + "\n")
    # 👑 [Ver 4.7] 4대 내러티브 필드 노출 (CIO 추론용)
    gap = r.get("expectation_gap")
    if gap:
        parts.append(f"   Expectation Gap: {gap}\n")
    causal = _parse_json_list(r.get("causal_chain"))
    if causal:
        parts.append("   Causal Chain: " + " -> ".join(str(c) for c in causal) + "\n")
    trk = _parse_json_list(r.get("tracking_indicators"))
    if trk:
        parts.append("   Tracking Indicators: " + "; ".join([f"{t.get('metric','')}@{t.get('threshold','')}" for t in trk if isinstance(t, dict)]) + "\n")
    tac_st = _parse_json_list(r.get("tactical_stance"))
    if tac_st:
        parts.append("   Tactical Stance: " + "; ".join([f"{t.get('asset','')}={t.get('stance','')}" for t in tac_st if isinstance(t, dict)]) + "\n")
    parts.append("\n")
    return "".join(parts)


async def aggregate_macro_context() -> str:
    """Invokes local MCP tools asynchronously to compile macro consensus and data.

    Ver 3.0 change:
      • All queries run in parallel via asyncio.gather (10x speedup vs serial)
      • Pagination is removed — the entire time-series corpus is returned
        so frontier 1M-context reasoners see every guru / every period.
      • Per-report payload is enriched with Ver 3.0 tactical signals
        (sector_tilt, duration_call, macro_factor) for cross-sectional analysis.
    """
    print("📂 [1/3] Aggregating macroeconomic knowledge from MCP Server (Ver 3.0 open-pull)...")

    # Parallel gather across all data dimensions — no LIMIT clauses.
    status_json, recent_json, contrarian_json = await asyncio.gather(
        mcp_server.get_pipeline_status(),
        mcp_server.get_recent_reports(),     # no limit → full corpus
        mcp_server.get_contrarian_opinions(),  # no limit → all contrarians
    )

    # 👑 [A16] MCP 실패 silent 치환 경고화 — "Error: ..." 반환 시 운영자 인지.
    def _safe_json(raw, default, label):
        if raw.startswith("Error"):
            print(f"   [WARN] MCP {label} failed — using empty default: {raw[:200]}")
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"   [WARN] MCP {label} JSON parse failed — using empty default: {e}")
            return default

    status_data = _safe_json(status_json, {}, "get_pipeline_status")
    recent_reports = _safe_json(recent_json, [], "get_recent_reports")
    contrarian_views = _safe_json(contrarian_json, [], "get_contrarian_opinions")

    # 👑 인사이트 보고서 유효기간 준용 — time_box 유효(미래/현재) + 빈값은 broadcast_date 90일.
    # 만료 전망(과거 타겟) + 90일 밖 과거 자료 제외 → CIO 전략이 현재 유효 전망에만 기반.
    from datetime import date as _date
    try:
        from src.insights.timebox import is_valid_time_box
    except Exception:
        from insights.timebox import is_valid_time_box  # type: ignore

    def _valid_view(v):
        tb = v.get("time_box", "")
        bd = v.get("broadcast_date", "")
        bd_date = None
        if bd:
            try:
                bd_date = _date.fromisoformat(str(bd)[:10])
            except Exception:
                bd_date = None
        return is_valid_time_box(tb, _date.today(), bd_date)

    if isinstance(recent_reports, list):
        recent_reports = [r for r in recent_reports if _valid_view(r)]
    if isinstance(contrarian_views, list):
        contrarian_views = [c for c in contrarian_views if _valid_view(c)]

    # Compile into a single textual buffer — single source of truth for the
    # frontier reasoner, no chunking, no summarization at this layer.
    context_str = f"## 📊 PIPELINE STATUS SUMMARY\n{json.dumps(status_data, indent=2, ensure_ascii=False)}\n\n"

    # 👑 [2026-08-06 M1] recent 섹션을 컨텍스트 예산의 ~70%로 캡.
    # 기존 head 200K 절삭은 섹션 순서상 RECENT(전체, LIMIT 없음)가 예산을 독점하면
    # 끝의 CONTRARIAN 섹션이 0% 포함됐음(DB 성장 시 비대칭/contrarian 분석이
    # 데이터 없이 생성 → 환각 유도). recent 를 예산 내로 제한해 contrarian 은
    # 항상 끝에 append 되도록 보존.
    MAX_CONTEXT_CHARS = 200_000
    RECENT_BUDGET_CHARS = int(MAX_CONTEXT_CHARS * 0.7)

    context_str += f"## 🎙️ RECENT EXPERT OPINIONS (budgeted ~{RECENT_BUDGET_CHARS:,} chars, n={len(recent_reports)})\n"
    if recent_reports and isinstance(recent_reports, list):
        recent_used = 0
        dropped = 0
        for idx, r in enumerate(recent_reports, 1):
            block = _render_report_block(idx, r)
            if recent_used and recent_used + len(block) > RECENT_BUDGET_CHARS:
                dropped = len(recent_reports) - idx + 1
                break
            context_str += block
            recent_used += len(block)
        if dropped:
            context_str += f"(RECENT 예산 초과 — 이후 {dropped}건 생략, contrarian 은 하단에 보존)\n\n"
    else:
        context_str += "No recent reports available.\n\n"

    context_str += f"## 🚨 CONTRARIAN / ASYMMETRICAL VIEWS (full corpus, n={len(contrarian_views)})\n"
    if contrarian_views and isinstance(contrarian_views, list):
        for idx, c in enumerate(contrarian_views, 1):
            context_str += _render_report_block(idx, c, contrarian=True)
    else:
        context_str += "No contrarian reports available.\n\n"

    print(f"   ✓ Assembled {len(recent_reports)} reports + {len(contrarian_views)} contrarians "
          f"into {len(context_str):,}-char context buffer.")
    # 최종 안전장치 — recent 가 예산 안이라도 contrarian 이 극단적으로 크면 초과 가능.
    # head 200K 유지(최신 우선), 과도한 contrarian 만 잘리도록 유지.
    if len(context_str) > MAX_CONTEXT_CHARS:
        print(f"   ⚠️ context buffer {len(context_str):,} > {MAX_CONTEXT_CHARS:,} — head {MAX_CONTEXT_CHARS:,}로 절삭(NIM ctx limit 보호).")
        context_str = context_str[:MAX_CONTEXT_CHARS]
    return context_str

# ---------------------------------------------------------------------------
# LLM Reasoning Engine Calls (NIM via nvidia-api-proxy, OpenAI SDK)
# ---------------------------------------------------------------------------
# 👑 OpenAI SDK 사용 — httpx 직접 POST /chat/completions 은 proxy 라우트 404.
# local_llm_client.py(Tier 1)와 동일 패턴. 동기 클라이언트를 asyncio.to_thread 로 비동기화.


def _call_nim_reasoner_sync(system: str, user: str) -> str:
    """동기 LLM 호출(asyncio.to_thread 로 비동기화) — Ollama Cloud 우선, NIM 폴백."""
    from src import cloud_client
    content = cloud_client.chat_completion(
        system=system,
        user=user,
        max_tokens=8192,
        temperature=0.3,
        nim_model=TIER3_MODEL,
        ollama_attempts=4,
    )
    if not content:
        raise RuntimeError(f"Ollama/NIM returned empty content for {TIER3_MODEL}")
    return content


def _extract_viz_json(report_content: str) -> dict:
    """리포트 본문에서 ```json 블록 추출 → dict. 실패 시 빈 dict."""
    import re
    m = re.search(r"```json\s*(\{.*?\})\s*```", report_content, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def _render_cio_visuals(viz_json: dict, out_dir: Path) -> dict:
    """CIO 시각화 3종 생성 → {pie, bar, conflicts} 경로. 실패 시 개별 None.
    A. 자산 배분 파이(plotly) B. 자산군 심리 바(cross_matrix) C. 핵심 갈등 다이어그램(pyvis)
    """
    paths = {"pie": None, "bar": None, "conflicts": None}
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import plotly.graph_objects as go
        # A. 자산 배분 파이
        alloc = viz_json.get("allocation", [])
        if alloc:
            labels = [a.get("asset", "?") for a in alloc]
            weights = [a.get("weight", 0) for a in alloc]
            fig = go.Figure(go.Pie(labels=labels, values=weights, hole=0.4,
                                   textinfo="label+percent", marker=dict(colors=["#0969da", "#1a7f37", "#bf8700", "#cf222e", "#8250df"])))
            fig.update_layout(title="CIO 다자산 배분 전략", height=500, width=700)
            p = out_dir / "cio_allocation_pie.html"
            fig.write_html(str(p), include_plotlyjs="cdn")
            paths["pie"] = p
    except Exception as e:
        logger.warning(f"파이 시각화 실패: {e}")

    try:
        # B. 자산군 심리 바 (cross_matrix 재사용)
        from src.insights.cross_matrix import asset_consensus_matrix
        df = asset_consensus_matrix()
        if not df.empty:
            import plotly.graph_objects as go
            d = df.head(12)
            fig = go.Figure(go.Bar(x=d["asset_class"], y=d["avg_bull_bear"], marker_color="#0969da",
                                    text=[f"{v:.1f}" for v in d["avg_bull_bear"]], textposition="outside"))
            fig.update_layout(title="자산군별 컨센서스 심리 (평균 bull/bear)", height=450, width=800,
                              yaxis_title="평균 심리 (/10)", xaxis_tickangle=-30)
            p = out_dir / "cio_sentiment_bar.html"
            fig.write_html(str(p), include_plotlyjs="cdn")
            paths["bar"] = p
    except Exception as e:
        logger.warning(f"심리 바 시각화 실패: {e}")

    try:
        # C. 핵심 갈등 다이어그램 (Long/Short 구루 노드-엣지)
        conflicts = viz_json.get("conflicts", [])
        if conflicts:
            import networkx as nx
            from pyvis.network import Network
            G = nx.Graph()
            for c in conflicts:
                topic = c.get("topic", "?")
                lg = c.get("long_guru", "?")
                sg = c.get("short_guru", "?")
                G.add_node(lg, color="#1a7f37", title=f"LONG: {c.get('long_view','')}", size=20)
                G.add_node(sg, color="#cf222e", title=f"SHORT: {c.get('short_view','')}", size=20)
                G.add_node(topic, color="#0969da", shape="diamond", size=15, title=f"갈등: {topic}")
                G.add_edge(lg, topic, label="Long")
                G.add_edge(sg, topic, label="Short")
            net = Network(height="500px", width="100%", notebook=False, bgcolor="#ffffff")
            net.from_nx(G)
            # 👑 [2026-08-07] 진동/겹침 방지 options — forceAtlas2Based + continuous smooth
            net.options = {
                "configure": {"enabled": False},
                "edges": {
                    "color": {"inherit": True},
                    "smooth": {"enabled": True, "type": "continuous"},
                },
                "interaction": {"dragNodes": True},
                "physics": {
                    "enabled": True,
                    "solver": "forceAtlas2Based",
                    "forceAtlas2Based": {
                        "gravitationalConstant": -50,
                        "centralGravity": 0.01,
                        "springLength": 100,
                        "springConstant": 0.08,
                        "damping": 0.4,
                        "avoidOverlap": 1,
                    },
                    "stabilization": {"iterations": 2000},
                },
            }
            p = out_dir / "cio_conflicts.html"
            net.write_html(str(p), notebook=False)
            # 안정화 완료 후 물리 엔진 off — 노드 위치 고정 (진동 제거)
            html = p.read_text(encoding="utf-8")
            if "stabilizationIterationsDone" not in html:
                script = (
                    '<script type="text/javascript">\n'
                    'network.once("stabilizationIterationsDone", function() {\n'
                    '    network.setOptions({ physics: false });\n'
                    '});\n'
                    '</script>\n</body>'
                )
                html = html.replace("</body>", script)
                p.write_text(html, encoding="utf-8")
            paths["conflicts"] = p
    except Exception as e:
        logger.warning(f"갈등 다이어그램 실패: {e}")

    return paths


def _viz_json_to_markdown(viz_json: dict) -> str:
    """viz_json(allocation/conflicts) → 보기 좋은 마크다운 표. 셀 내 '|' 이스케이프."""
    def esc(v) -> str:
        return str(v or "").replace("|", "\\|").replace("\n", " ").strip()

    lines: list[str] = []
    alloc = viz_json.get("allocation", [])
    if alloc:
        lines.append("### 📊 자산 배분 요약")
        lines.append("| 자산 | 비중 | 핵심 근거 |")
        lines.append("|------|----:|------|")
        for a in alloc:
            lines.append(f"| {esc(a.get('asset'))} | {esc(a.get('weight'))}% | {esc(a.get('rationale'))} |")
        lines.append("")

    conflicts = viz_json.get("conflicts", [])
    if conflicts:
        lines.append("### ⚔️ 핵심 갈등 요약")
        lines.append("| 갈등 주제 | 롱 구루 | 롱 주장 | 숏 구루 | 숏 주장 |")
        lines.append("|----------|---------|---------|---------|---------|")
        for c in conflicts:
            lines.append(
                f"| {esc(c.get('topic'))} | {esc(c.get('long_guru'))} | {esc(c.get('long_view'))} "
                f"| {esc(c.get('short_guru'))} | {esc(c.get('short_view'))} |"
            )
        lines.append("")
    return "\n".join(lines)


def _replace_json_block_with_tables(report_content: str, viz_json: dict) -> str:
    """본문의 ```json 블록을 마크다운 표로 교체. viz_json 없으면 제거만."""
    import re
    table_md = _viz_json_to_markdown(viz_json) if viz_json else ""
    return re.sub(r"```json\s*\{.*?\}\s*```", table_md, report_content, flags=re.DOTALL).strip()


async def query_reasoner_llm(context_data: str) -> str:
    """Queries the shared Ollama Cloud/NIM fallback route."""
    print(f"🤖 [2/3] Dispatching to LLM (NIM fallback: {TIER3_MODEL})...")

    system_instruction = (
        "당신은 최고 수준의 최고투자책임자(CIO)이자 시니어 글로벌 매크로 전략가입니다.\n"
        "주어진 시장 컨센서스, 전문가 의견, 컨트리안(반대) 관점을 분석하여 기관급 매크로 자산배분 및 전략 리포트를 작성하세요.\n\n"
        "리포트는 반드시 다음 섹션을 포함해야 합니다:\n"
        "1. 요약 및 매크로 레짐 평가 (불/베어 컨센서스)\n"
        "2. 이견 및 컨트리안 분석 (비대칭 위험/보상 기회)\n"
        "3. 다자산 배분 전략 (주식, 채권, 원자재, 현금/FX에 대한 구체적 관점)\n"
        "4. 핵심 무효화 촉매 (이 자산배분이 틀렸음을 증명할 트리거)\n\n"
        "표준 마크다운 형식과 명확한 헤딩을 사용하세요. 분석적이고 전문적이며 단정적인 어조를 유지하세요.\n\n"
        "👑 [Ver 4.4] 근거 기반 작성 지침:\n"
        "- 입력 컨텍스트의 Verbatim Quote / Additional Quote 를 근거로 인용할 때는 따옴표 그대로 사용하고, 새 인용을 창작하지 마세요.\n"
        "- 섹션 4 '핵심 무효화 촉매'는 입력의 Invalidation Risks / Conditional Catalysts 데이터를 우선 반영하세요.\n"
        "- allocation 의 rationale 은 sector_tilt / duration_call / view_time_horizon / Key Data Points / Price Targets 를 근거로 작성하세요.\n"
        "- 제공되지 않은 수치나 가격 목표를 새로 만들지 마세요.\n\n"
        "👑 시각화용 JSON: 리포트 마지막에 반드시 아래 형식의 ```json 블록을 포함하세요 (시각화 자동 생성용):\n"
        "```json\n"
        "{\n"
        "  \"allocation\": [{\"asset\": \"주식\", \"weight\": 50, \"rationale\": \"한 줄 근거\"}, ...],\n"
        "  \"conflicts\": [{\"topic\": \"갈등 주제\", \"long_guru\": \"구루명\", \"long_view\": \"한 줄\", \"short_guru\": \"구루명\", \"short_view\": \"한 줄\"}, ...]\n"
        "}\n"
        "```\n"
        "allocation 은 합이 100이 되는 자산별 비중(%). conflicts 는 이견 섹션의 핵심 Long/Short 대립 1-3개. 구루명은 실제 데이터의 speaker_name 사용."
    )

    prompt = (
        "다음은 집계된 거시경제 지식그래프 컨텍스트입니다:\n"
        "============================================================\n"
        f"{context_data}\n"
        "============================================================\n\n"
        "위 컨센서스를 기반으로 매크로 자산배분 리포트를 생성해 주세요."
    )

    # Provider retries/fallback run once inside the shared execution layer.
    result = await asyncio.to_thread(_call_nim_reasoner_sync, system_instruction, prompt)
    result = result.strip()
    if not result:
        raise RuntimeError(f"LLM returned empty content for {TIER3_MODEL}")
    return result

# ---------------------------------------------------------------------------
# Report Saving Step
# ---------------------------------------------------------------------------
def save_report_to_vault(report_content: str) -> Path:
    """Saves the generated reasoning report into the Obsidian Vault under reports/ folder."""
    print("💾 [3/3] Saving generated report to Obsidian Vault...")
    
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    reports_folder = VAULT_PATH / "reports"
    reports_folder.mkdir(parents=True, exist_ok=True)
    
    report_file = reports_folder / f"Grand_Report_{today_str}.md"
    
    metadata_header = f"""---
date: {today_str}
type: grand_reasoning_report
model: {TIER3_MODEL}
provider: nim
tags: [macro_strategy, global_macro, asset_allocation]
---

"""
    # 본문의 ```json 블록을 보기 좋은 마크다운 표로 교체(원본 JSON 제거).
    viz_json = _extract_viz_json(report_content)
    clean_content = _replace_json_block_with_tables(report_content, viz_json)
    full_document = metadata_header + clean_content

    report_file.write_text(full_document, encoding="utf-8")
    return report_file

# ---------------------------------------------------------------------------
# E2E Execution Orchestration
# ---------------------------------------------------------------------------
def _send_cio_email_with_visuals(subject: str, body_md: str, viz_paths: dict) -> None:
    """CIO 리포트 메일 — 본문(마크다운→HTML) + 시각화 HTML 첨부. 실패 시 plain 폴백."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    from src.report_generator import _md_to_html_email, _resolve_recipients

    user = settings.email.user
    pwd = settings.email.password
    if not user or not pwd:
        print("[INFO] Gmail 설정 없음 — 메일 스킵")
        return
    recipients = _resolve_recipients()
    pwd_clean = pwd.replace(" ", "")
    host = settings.email.smtp_host
    port = settings.email.smtp_port

    html_body = _md_to_html_email(body_md)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_md, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    # 시각화 HTML 첨부 (브라우저에서 인터랙티브)
    for key, fname in [("pie", "cio_allocation_pie.html"), ("bar", "cio_sentiment_bar.html"), ("conflicts", "cio_conflicts.html")]:
        p = viz_paths.get(key)
        if p and Path(p).is_file():
            with open(p, "rb") as f:
                att = MIMEApplication(f.read(), _subtype="html")
            att.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(att)

    try:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(user, pwd_clean)
            s.sendmail(user, recipients, msg.as_string())
        n_att = sum(1 for v in viz_paths.values() if v)
        print(f"✓ CIO 메일 발송 (본문 HTML + 시각화 첨부 {n_att}) → {', '.join(recipients)}")
    except Exception as e:
        print(f"⚠️ 시각화 메일 실패({e}) — plain 폴백")
        send_email_report(subject, body_md)


async def run_orchestrator():
    start_time = time.time()
    try:
        context_data = await aggregate_macro_context()
        report_content = await query_reasoner_llm(context_data)
        saved_path = save_report_to_vault(report_content)

        # 👑 시각화 3종 (A 자산배분 파이 / B 자산심리 바 / C 핵심갈등 다이어그램)
        viz_json = _extract_viz_json(report_content)
        viz_dir = VAULT_PATH / "insights"
        viz_paths = _render_cio_visuals(viz_json, viz_dir)

        # 본문에 시각화 링크 섹션 추가 (Obsidian/로컬 확인용)
        viz_links = []
        for k, label in [("pie", "자산 배분 파이"), ("bar", "자산 심리 바"), ("conflicts", "핵심 갈등 다이어그램")]:
            p = viz_paths.get(k)
            if p:
                viz_links.append(f"- **{label}**: {p.as_posix()}")
        if viz_links:
            viz_section = "\n\n---\n\n## 📊 시각화\n\n" + "\n".join(viz_links) + "\n"
            report_content_with_viz = report_content + viz_section
        else:
            report_content_with_viz = report_content

        # 👑 메일 발송 — 본문(narrative) + 시각화 HTML 첨부
        today_str = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)).date().strftime("%Y-%m-%d")
        email_subject = f"📊 주간 매크로 CIO 전략 리포트 (Grand Reasoning Report) - {today_str}"
        _send_cio_email_with_visuals(email_subject, report_content_with_viz, viz_paths)

        elapsed = time.time() - start_time
        print("=" * 60)
        print("🎉 Grand Frontier Reasoner Report Completed Successfully!")
        print(f"   Saved to: {saved_path.as_posix()}")
        n_viz = sum(1 for v in viz_paths.values() if v)
        print(f"   시각화: {n_viz}/3종 생성 (파이/바/갈등)")
        print(f"   Time elapsed: {elapsed:.2f} seconds")
        print("   📨 Email sent to GMAIL_USER (주간 CIO 전략 리포트 + 시각화 첨부)")
        print("=" * 60)
    except Exception as e:
        print(f"❌ Orchestration failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_orchestrator())
