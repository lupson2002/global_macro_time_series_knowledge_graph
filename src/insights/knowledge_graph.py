"""지식그래프 — nodes 공동등장 기반 networkx 가중 그래프 + 커뮤니티 + 시각화.

각 video 내 노드 공동등장 = 엣지, weight = 공동등장 video 수.
커뮤니티 탐지(louvain), 중심성, pyvis 인터랙티브 HTML + plotly 대시보드.
"""
from __future__ import annotations

import sqlite3
from itertools import combinations
from pathlib import Path

import networkx as nx

from .normalize import normalize_node
from .timebox import valid_time_box_values

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "macro_knowledge.db"
VAULT_INSIGHTS = (
    Path(__file__).resolve().parent.parent.parent / "obsidian_vault" / "insights"
)

# 그래프에 포함할 node_type (티커는 너무 파편화 → asset + theme 우선, ticker 옵션)
DEFAULT_TYPES = ("asset_class", "macro_theme")
MIN_EDGE_WEIGHT = 2  # 최소 2개 video 공동등장
MIN_NODE_FREQ = 3     # 최소 3회 등장 노드


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_nodes(node_types: tuple[str, ...], use_timebox: bool = True) -> dict[str, list[tuple[str, str]]]:
    """video_id → [(norm_value, node_type)]. 정규화 적용. use_timebox 시 time_box 유효 video만."""
    conn = _connect()
    try:
        if use_timebox:
            valid = valid_time_box_values()
            empty_clause = "(r.time_box IS NULL OR r.time_box = '') AND r.broadcast_date >= date('now','-90 days')"
            if valid:
                placeholders = ",".join("?" * len(valid))
                where = (
                    f"WHERE n.node_type IN ({','.join('?' * len(node_types))}) "
                    f"AND (r.time_box IN ({placeholders}) OR ({empty_clause}))"
                )
                params = list(node_types) + list(valid)
            else:
                where = (
                    f"WHERE n.node_type IN ({','.join('?' * len(node_types))}) "
                    f"AND ({empty_clause})"
                )
                params = list(node_types)
            sql = f"SELECT n.video_id, n.node_type, n.node_value FROM nodes n JOIN reports r ON n.video_id = r.video_id {where}"
            rows = conn.execute(sql, params).fetchall()
        else:
            rows = conn.execute(
                "SELECT video_id, node_type, node_value FROM nodes WHERE node_type IN ({})".format(
                    ",".join("?" * len(node_types))
                ),
                node_types,
            ).fetchall()
    finally:
        conn.close()
    by_video: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        nv = normalize_node(r["node_value"], r["node_type"])
        if not nv:
            continue
        by_video.setdefault(r["video_id"], []).append((nv, r["node_type"]))
    return by_video


def build_cooccurrence_graph(
    node_types: tuple[str, ...] = DEFAULT_TYPES,
    min_edge_weight: int = MIN_EDGE_WEIGHT,
    min_node_freq: int = MIN_NODE_FREQ,
    use_timebox: bool = True,
) -> nx.Graph:
    """공동등장 기반 가중 그래프. 노드 속성: type, freq. 엣지 속성: weight."""
    by_video = _load_nodes(node_types, use_timebox=use_timebox)

    # 노드 빈도
    freq: dict[str, int] = {}
    node_type_map: dict[str, str] = {}
    for nodes in by_video.values():
        seen = set()
        for nv, nt in nodes:
            if nv in seen:
                continue
            seen.add(nv)
            freq[nv] = freq.get(nv, 0) + 1
            node_type_map[nv] = nt

    # 엣지 (같은 video 내 unique 노드 쌍)
    edge_weight: dict[tuple[str, str], int] = {}
    for nodes in by_video.values():
        uniq = sorted({nv for nv, _ in nodes})
        for a, b in combinations(uniq, 2):
            key = (a, b)
            edge_weight[key] = edge_weight.get(key, 0) + 1

    G = nx.Graph()
    for nv, f in freq.items():
        if f >= min_node_freq:
            G.add_node(nv, type=node_type_map[nv], freq=f)
    for (a, b), w in edge_weight.items():
        if w >= min_edge_weight and G.has_node(a) and G.has_node(b):
            G.add_edge(a, b, weight=w)
    return G


def detect_communities(G: nx.Graph) -> dict[str, int]:
    """Louvain 커뮤니티 (networkx >=3.6). 실패 시 greedy fallback."""
    try:
        from networkx.algorithms.community import louvain_communities

        comms = louvain_communities(G, weight="weight", seed=42)
    except Exception:
        comms = nx.community.greedy_modularity_communities(G, weight="weight")
    node_comm: dict[str, int] = {}
    for i, comm in enumerate(comms):
        for n in comm:
            node_comm[n] = i
    return node_comm


def centralities(G: nx.Graph) -> dict[str, dict[str, float]]:
    """degree/betweenness 중심성."""
    return {
        "degree": dict(nx.degree_centrality(G)),
        "betweenness": dict(nx.betweenness_centrality(G, weight="weight")),
    }


def render_pyvis(G: nx.Graph, node_comm: dict[str, int], out_path: Path | None = None) -> Path:
    """pyvis 인터랙티브 HTML. 노드 크기=freq, 엣지 굵기=weight, 색=커뮤니티."""
    from pyvis.network import Network

    VAULT_INSIGHTS.mkdir(parents=True, exist_ok=True)
    out = out_path or (VAULT_INSIGHTS / "knowledge_graph.html")

    net = Network(height="750px", width="100%", notebook=False, bgcolor="#ffffff")
    palette = ["#0969da", "#cf222e", "#1a7f37", "#bf8700", "#8250df", "#1f6feb", "#a371f7"]

    for n, d in G.nodes(data=True):
        comm = node_comm.get(n, 0)
        color = palette[comm % len(palette)]
        size = 10 + min(d.get("freq", 1) * 2, 40)
        net.add_node(n, label=n, size=size, color=color, title=f"{d.get('type','')} · freq={d.get('freq',1)} · comm={comm}")
    for a, b, d in G.edges(data=True):
        net.add_edge(a, b, value=d.get("weight", 1), width=min(d.get("weight", 1) * 0.5, 5))
    net.write_html(str(out), notebook=False)
    return out


def _build_dashboard_fig(matrices: dict, G: nx.Graph, node_comm: dict[str, int]):
    """plotly 대시보드 Figure 빌드 (HTML/PNG 공용)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    asset_df = matrices.get("asset")
    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=("자산군별 평균 심리", "주도 테마 (빈도×심리)", "그래프 요약", "의견 분기(상위 5)"),
        specs=[[{"type": "xy"}, {"type": "xy"}], [{"type": "table"}, {"type": "xy"}]],
    )
    if asset_df is not None and not asset_df.empty:
        fig.add_trace(go.Bar(x=asset_df["asset_class"], y=asset_df["avg_bull_bear"], marker_color="#0969da"), row=1, col=1)
    theme_df = matrices.get("theme")
    if theme_df is not None and not theme_df.empty:
        fig.add_trace(go.Bar(x=theme_df["macro_theme"], y=theme_df["avg_bull_bear"], marker_color="#1a7f37"), row=1, col=2)
    fig.add_trace(go.Table(
        header=dict(values=["지표", "값"]),
        cells=dict(values=[["노드 수", "엣지 수", "커뮤니티 수"], [G.number_of_nodes(), G.number_of_edges(), len(set(node_comm.values()))]]),
    ), row=2, col=1)
    div_df = matrices.get("divergence")
    if div_df is not None and not div_df.empty:
        fig.add_trace(go.Bar(x=div_df["asset_class"].head(5), y=div_df["stddev_bull_bear"].head(5), marker_color="#cf222e"), row=2, col=2)
    fig.update_layout(height=800, width=1200, title_text="QuantMind 인사이트 대시보드", showlegend=False)
    return fig


def render_plotly_dashboard(matrices: dict, G: nx.Graph, node_comm: dict[str, int], out_path: Path | None = None) -> Path:
    """정적 HTML 대시보드 — 자산 심리 바 + 테마 클러스터 산점 + 그래프 요약."""
    VAULT_INSIGHTS.mkdir(parents=True, exist_ok=True)
    out = out_path or (VAULT_INSIGHTS / "insight_dashboard.html")
    fig = _build_dashboard_fig(matrices, G, node_comm)
    fig.write_html(str(out), include_plotlyjs="cdn")
    return out


def render_plotly_png(matrices: dict, G: nx.Graph, node_comm: dict[str, int], out_path: Path | None = None) -> Path:
    """대시보드 PNG (이메일 inline 이미지용). kaleido 필요."""
    VAULT_INSIGHTS.mkdir(parents=True, exist_ok=True)
    out = out_path or (VAULT_INSIGHTS / "insight_dashboard.png")
    fig = _build_dashboard_fig(matrices, G, node_comm)
    img = fig.to_image(format="png", width=1200, height=800, scale=2)
    out.write_bytes(img)
    return out


if __name__ == "__main__":
    G = build_cooccurrence_graph()
    comm = detect_communities(G)
    print(f"노드 {G.number_of_nodes()} 엣지 {G.number_of_edges()} 커뮤니티 {len(set(comm.values()))}")
    deg = sorted(dict(nx.degree_centrality(G)).items(), key=lambda x: -x[1])[:10]
    print("중심성 상위:", deg)
    out = render_pyvis(G, comm)
    print("pyvis:", out)