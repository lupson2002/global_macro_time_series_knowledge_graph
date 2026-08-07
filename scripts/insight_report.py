#!/usr/bin/env python3
"""정기 인사이트 리포트 — 크로스 매트릭스 + 지식그래프 + RAG 인사이트 취합 → 마크다운 + plotly 대시보드 → 메일 발송.

Usage:
    .venv/bin/python scripts/insight_report.py            # 전체 + 메일
    .venv/bin/python scripts/insight_report.py --no-send   # 리포트만
    .venv/bin/python scripts/insight_report.py --no-llm    # LLM/RAG 스킵 (빠른 산출)
"""
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.insights.cross_matrix import build_all_matrices, df_to_markdown
from src.insights.knowledge_graph import (
    build_cooccurrence_graph,
    build_visualization_graph,
    detect_communities,
    render_pyvis,
    render_force_graph_3d,
    render_plotly_dashboard,
    summarize_network,
)


def build_report(no_llm: bool = False, expiry: str = "timebox") -> tuple[str, dict]:
    """마크다운 리포트 빌드. expiry: timebox(기본, time_box 유효기간) | all(전체). (md, summary)."""
    import networkx as nx

    use_timebox = expiry == "timebox"

    print(f"📊 [1/4] 크로스 집계 매트릭스... (expiry={expiry})")
    matrices = build_all_matrices(use_timebox=use_timebox)

    print("🕸️ [2/4] 지식그래프 구축...")
    G = build_cooccurrence_graph(use_timebox=use_timebox)
    comm = detect_communities(G)
    n_nodes, n_edges, n_comms = G.number_of_nodes(), G.number_of_edges(), len(set(comm.values()))
    deg_top = sorted(dict(nx.degree_centrality(G)).items(), key=lambda x: -x[1])[:8]

    # 시각화용 정제 그래프 (가독성 최적화: 고립/리프 제거 + 중심성)
    Gv, comm_v, cent_v = build_visualization_graph(G)
    nv_nodes, nv_edges = Gv.number_of_nodes(), Gv.number_of_edges()
    summarize_network(Gv, comm_v)

    print("🎨 [3/4] 시각화(pyvis 2D + 3d-force-graph 3D + 대시보드)...")
    pyvis_path = render_pyvis(Gv, comm_v, cent_v)
    three_d_path = render_force_graph_3d(Gv, comm_v, cent_v)
    dash_path = render_plotly_dashboard(matrices, G, comm)

    rag_insights: dict[str, str] = {}
    key_conclusions: str = ""
    if not no_llm:
        print("🤖 [4/4] RAG/LLM 인사이트 (핵심결론 + 질의별)...")
        from src.insights.rag_insights import build_insights, generate_key_conclusions
        key_conclusions = generate_key_conclusions(matrices, use_timebox=use_timebox)
        rag_insights = build_insights(use_timebox=use_timebox)
    else:
        print("🤖 [4/4] LLM 스킵 (--no-llm)")

    # 마크다운 조립
    L = []
    today = datetime.now().strftime("%Y-%m-%d")
    L.append(f"# 🧠 QuantMind 인사이트 리포트 ({today})")
    L.append("")
    L.append(f"- 생성: {datetime.now().strftime('%Y-%m-%d %H:%M KST')}")
    L.append(f"- **유효기간 필터**: {expiry} ({'time_box 유효(미래/현재) + 빈값 90일, 만료 제외' if use_timebox else '전체 DB'})")
    L.append(f"- 그래프: 노드 {n_nodes} / 엣지 {n_edges} / 커뮤니티 {n_comms}")
    L.append("- 시각화: pyvis/plotly HTML 첨부(브라우저에서 열면 인터랙티브)")
    L.append("")
    L.append("---")
    L.append("")

    # 👑 핵심결론 최상단 배치 (투자/회피/유니크)
    if key_conclusions:
        L.append("## 🎯 핵심 투자 결론")
        L.append("")
        L.append(key_conclusions.strip())
        L.append("")
    L.append("---")
    L.append("")

    L.append("## 1. 자산군별 컨센서스")
    L.append(f"> 💡 {_matrix_headline(matrices['asset'], 'asset')}")
    L.append("")
    L.append(df_to_markdown(matrices["asset"].head(15), "평균 심리 내림차순 (n≥5)"))
    L.append("## 2. 주도 테마")
    L.append(f"> 💡 {_matrix_headline(matrices['theme'], 'theme')}")
    L.append("")
    L.append(df_to_markdown(matrices["theme"].head(15), "macro_theme 정규화군"))
    L.append("## 3. 채널별 컨센서스/contrarian")
    L.append(f"> 💡 {_matrix_headline(matrices['channel'], 'channel')}")
    L.append("")
    L.append(df_to_markdown(matrices["channel"], "source_channel 정규화"))
    L.append("## 4. 의견 분기 (잠재 contrarian 기회)")
    L.append(f"> 💡 {_matrix_headline(matrices['divergence'], 'divergence')}")
    L.append("")
    L.append(df_to_markdown(matrices["divergence"].head(10), "stddev 상위 = 의견 분기 큰 자산"))

    # 👑 [주간] Friction Index TOP3 — 의견 대립 × 확신도 (변곡/불확실성 자산)
    try:
        from scripts.insights.weekly_analytics import friction_index
        fi = friction_index(use_timebox=use_timebox)
        if fi:
            L.append("### ⚙️ 마찰 지수 TOP3 (확신 높은 의견 대립)")
            L.append("| 자산군 | stddev | 평균 확신도 | 마찰 지수 | n |")
            L.append("|---|---|---|---|---|")
            for x in fi:
                L.append(f"| {x['asset']} | {x['stddev']} | {x['conviction']} | **{x['friction']}** | {x['n']} |")
            L.append("")
    except Exception:
        pass

    L.append("## 5. 지식그래프 요약")
    L.append("")
    L.append(f"- 노드 {n_nodes} / 엣지 {n_edges} / 커뮤니티 {n_comms}")
    L.append(f"- 시각화 정제: 노드 {nv_nodes} / 엣지 {nv_edges} (min_degree=2, min_edge_weight=2, 고립/리프 제거)")
    L.append("- 중심성 상위(자산↔테마 연결 허브):")
    for n, c in deg_top:
        L.append(f"  - **{n}**: {c:.3f}")
    L.append("")
    L.append(f"인터랙티브 그래프: {pyvis_path.as_posix()}")
    L.append(f"대시보드: {dash_path.as_posix()}")
    L.append("")

    # 👑 [주간] 인과 체인 근본 원인 (PageRank) — 지식그래프에 반영
    try:
        from scripts.insights.weekly_analytics import causal_centrality
        cc = causal_centrality(top_k=5)
        if cc:
            L.append("### 🔗 인과 체인 근본 원인 (PageRank Root Bottleneck)")
            for x in cc:
                L.append(f"- **{x['node']}** (pagerank {x['pagerank']})")
            L.append("")
    except Exception:
        pass

    if rag_insights:
        L.append("## 6. RAG/LLM 인사이트")
        L.append("")
        for q, insight in rag_insights.items():
            L.append(f"### {q}")
            L.append("")
            L.append(insight.strip())
            L.append("")

    L.append("---")
    L.append("")
    L.append("*정규화: 동의어/대소문자 병합(NVDA/Nvidia/NVIDIA→NVDA, AI/Artificial Intelligence→AI 등). 비파괴.*")

    md = "\n".join(L)
    summary = {
        "nodes": n_nodes, "edges": n_edges, "communities": n_comms,
        "rag_queries": len(rag_insights),
        "has_key_conclusions": bool(key_conclusions),
        "pyvis_path": pyvis_path, "three_d_path": three_d_path, "dashboard_path": dash_path,
    }
    return md, summary


def send_email_with_visuals(md: str, summary: dict, subject: str) -> None:
    """마크다운 → HTML + 대시보드 PNG inline + pyvis/plotly HTML 첨부 발송."""
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    from dotenv import load_dotenv
    from src.report_generator import _md_to_html_email, send_email_report, _resolve_recipients  # noqa: F401 (fallback)

    load_dotenv(PROJECT_ROOT / ".env")
    user = os.environ.get("GMAIL_USER") or os.environ.get("SMTP_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("SMTP_PASS")
    recipients = _resolve_recipients()
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    if not all([user, pwd, recipients]):
        print("SMTP 설정 부족 — 리포트만 생성, 메일 미발송")
        return

    html_body = _md_to_html_email(md)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)

    # alternative: plain + html
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(md, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    # 본문 PNG inline 제거 — 본문은 표(마크다운)만. 시각화는 첨부.
    # 첨부: pyvis 2D + plotly 3D 그래프 + plotly 대시보드 HTML (브라우저에서 인터랙티브)
    n_att = 0
    for key, fname in (("pyvis_path", "knowledge_graph.html"), ("three_d_path", "knowledge_graph_3d.html"), ("dashboard_path", "insight_dashboard.html")):
        p = summary.get(key)
        if p and Path(p).is_file():
            with open(p, "rb") as f:
                att = MIMEApplication(f.read(), _subtype="html")
            att.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(att)
            n_att += 1

    try:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(user, pwd)
            s.sendmail(user, recipients, msg.as_string())
        print(f"✓ 메일 발송 완료 (본문 표 + HTML 첨부 {n_att}) → {', '.join(recipients)}")
    except Exception as e:
        print(f"⚠️ 시각화 메일 발송 실패({e}) — plain 텍스트 폴백 발송")
        send_email_report(subject, md)  # report_generator plain 발송 폴백


def _matrix_headline(df, kind: str) -> str:
    """매트릭스 df 에서 핵심 한 줄 도출. 빈 df 면 안내문."""
    if df is None or df.empty:
        return "데이터 부족으로 핵치 도출 불가"
    if kind == "asset":
        col = "asset_class"
        top_bull = df.iloc[0]
        bear = df.sort_values("avg_bull_bear").iloc[0]
        contra = df.sort_values("contrarian_pct", ascending=False).iloc[0]
        return (f"**{top_bull[col]}** {top_bull['avg_bull_bear']:.1f} 최고 bull · "
                f"**{bear[col]}** {bear['avg_bull_bear']:.1f} 최고 bear · "
                f"**{contra[col]}** contrarian {contra['contrarian_pct']:.0f}%")
    if kind == "theme":
        col = "macro_theme"
        top_freq = df.sort_values("n", ascending=False).iloc[0]
        bear = df.sort_values("avg_bull_bear").iloc[0]
        return (f"**{top_freq[col]}** {int(top_freq['n'])}건 최다 언급 · "
                f"**{bear[col]}** {bear['avg_bull_bear']:.1f} bear 경사 테마")
    if kind == "channel":
        col = "channel"
        bull = df.iloc[0]
        contra = df.sort_values("contrarian_pct", ascending=False).iloc[0]
        return (f"**{bull[col]}** 평균 {bull['avg_bull_bear']:.2f} 최고 bull · "
                f"**{contra[col]}** contrarian {contra['contrarian_pct']:.0f}% "
                f"{'반체론 편중' if contra['contrarian_pct'] >= 50 else '상대적 회의'}")
    if kind == "divergence":
        col = "asset_class"
        top = df.iloc[0]
        return (f"**{top[col]}** stddev {top['stddev_bull_bear']:.2f} = 의견 가장 갈림 → "
                f"잠재 contrarian 기회 (평균 {top['avg_bull_bear']:.1f})")
    return ""


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--expiry", choices=["timebox", "all"], default="timebox",
                    help="timebox=time_box 유효기간(만료 제외, 기본) | all=전체 DB")
    args = ap.parse_args()

    md, summary = build_report(no_llm=args.no_llm, expiry=args.expiry)

    reports_dir = PROJECT_ROOT / "reports" / "insights"
    reports_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out = reports_dir / f"insight_report_{today}.md"
    out.write_text(md, encoding="utf-8")
    print(f"\n✓ 리포트: {out} ({out.stat().st_size} bytes)")
    print(f"  그래프: 노드 {summary['nodes']} / 엣지 {summary['edges']} / 커뮤 {summary['communities']} / RAG {summary['rag_queries']}")

    if not args.no_send:
        subject = f"🧠 QuantMind 인사이트 리포트 - {today}"
        send_email_with_visuals(md, summary, subject)


if __name__ == "__main__":
    main()