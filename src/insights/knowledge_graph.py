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


# ── 👑 [2026-08-07] 가독성 최적화: 정제(Pruning) + 중심성 + 커뮤니티 ──
def prune_graph(G: nx.Graph, min_degree: int = 2, min_edge_weight: int = 2) -> nx.Graph:
    """가변 필터로 그래프 정제 (레이아웃 단순화).
    - min_edge_weight 미만 엣지 제거
    - min_degree 미만 노드 제거 (고립 노드 + 1개 간선 리프 노드 기본 제거)
    """
    G2 = G.copy()
    G2.remove_edges_from(
        [(a, b) for a, b, d in G2.edges(data=True) if d.get("weight", 0) < min_edge_weight]
    )
    G2.remove_nodes_from([n for n, deg in dict(G2.degree()).items() if deg < min_degree])
    return G2


def compute_centralities(G: nx.Graph) -> dict[str, dict[str, float]]:
    """PageRank + Betweenness 중심성 (가중 그래프)."""
    return {
        "pagerank": dict(nx.pagerank(G, weight="weight")),
        "betweenness": dict(nx.betweenness_centrality(G, weight="weight")),
    }


def _scale_size(cent_val: float, freq: int, max_cent: float, min_s: int = 8, max_s: int = 40) -> float:
    """중심성 정규화 → 노드 크기. max_cent<=0(빈 그래프)이면 freq 기반 폴백."""
    if max_cent <= 0:
        return min_s + min(freq * 1.5, max_s - min_s)
    return min_s + (cent_val / max_cent) * (max_s - min_s)


def build_visualization_graph(
    G: nx.Graph, min_degree: int = 2, min_edge_weight: int = 2
) -> tuple[nx.Graph, dict[str, int], dict[str, dict[str, float]]]:
    """시각화용 정제 그래프 일괄 준비 → (G_pruned, node_comm, centralities)."""
    Gp = prune_graph(G, min_degree=min_degree, min_edge_weight=min_edge_weight)
    comm = detect_communities(Gp)
    cent = compute_centralities(Gp)
    return Gp, comm, cent


def summarize_network(G: nx.Graph, node_comm: dict[str, int], top_k: int = 10) -> None:
    """Top K 핵심 노드 + 커뮤니티 요약 콘솔 출력 (PageRank+Betweenness 복합 점수)."""
    cent = compute_centralities(G)
    pr, bt = cent["pagerank"], cent["betweenness"]
    pr_max = max(pr.values()) if pr else 1.0
    bt_max = max(bt.values()) if bt else 1.0
    score = {n: (pr.get(n, 0) / pr_max) + (bt.get(n, 0) / bt_max) for n in G.nodes()}

    print("\n" + "=" * 66)
    print(f"🏆 Top {top_k} 핵심 노드 (PageRank + Betweenness 복합)")
    print("=" * 66)
    for i, (n, s) in enumerate(sorted(score.items(), key=lambda x: -x[1])[:top_k], 1):
        print(f"{i:2d}. {n:<22} PR={pr.get(n,0):.4f} BT={bt.get(n,0):.4f} "
              f"comm={node_comm.get(n,0)} type={G.nodes[n].get('type','')}")

    groups: dict[int, list[str]] = {}
    for n, c in node_comm.items():
        groups.setdefault(c, []).append(n)
    print("\n" + "=" * 66)
    print(f"🗂️ 커뮤니티 요약 ({len(groups)}개)")
    print("=" * 66)
    for c, members in sorted(groups.items(), key=lambda x: -len(x[1])):
        top = sorted(members, key=lambda n: score.get(n, 0), reverse=True)[:3]
        print(f"  커뮤니티 {c}: {len(members):3d} 노드 | 대표: {', '.join(top)}")
    print()


def render_pyvis(
    G: nx.Graph,
    node_comm: dict[str, int],
    cent: dict[str, dict[str, float]] | None = None,
    out_path: Path | None = None,
) -> Path:
    """pyvis 인터랙티브 2D — 가독성 최적화.
    - 노드 크기 = PageRank 중심성 비례 (동적 스케일링)
    - 색 = Louvain 커뮤니티 팔레트
    - Ego-Network 하이라이트 (클릭 → 1/2차 이웃만 강조, 나머지 반투명)
    - 노드 검색 + 커뮤니티 필터 드롭다운
    - forceAtlas2Based 안정화 완료 후 physics off (진동 방지)
    """
    from pyvis.network import Network

    VAULT_INSIGHTS.mkdir(parents=True, exist_ok=True)
    out = out_path or (VAULT_INSIGHTS / "knowledge_graph.html")

    if cent is None:
        cent = compute_centralities(G)
    pr = cent.get("pagerank", {})
    pr_max = max(pr.values()) if pr else 0.0

    net = Network(height="750px", width="100%", notebook=False, bgcolor="#ffffff")
    palette = ["#0969da", "#cf222e", "#1a7f37", "#bf8700", "#8250df", "#1f6feb", "#a371f7"]

    for n, d in G.nodes(data=True):
        comm = node_comm.get(n, 0)
        color = palette[comm % len(palette)]
        size = _scale_size(pr.get(n, 0), d.get("freq", 1), pr_max)
        net.add_node(n, label=n, size=size, color=color, comm=comm,
                     title=f"{d.get('type','')} · freq={d.get('freq',1)} · comm={comm} · PR={pr.get(n,0):.4f}")
    for a, b, d in G.edges(data=True):
        net.add_edge(a, b, value=d.get("weight", 1), width=min(d.get("weight", 1) * 0.5, 5))

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
    net.write_html(str(out), notebook=False)
    # 인터랙티브 JS 주입 (Ego-Network + 검색 + 커뮤니티 필터)
    # 주의: pyvis 자체 템플릿이 stabilizationIterationsDone 핸들러를 이미 포함하므로
    # 중복 주입 방지 마커는 내 스크립트 고유 문자열(getConnectedNodes)로 판별.
    html = out.read_text(encoding="utf-8")
    if "getConnectedNodes" not in html:
        html = html.replace("</body>", _build_pyvis_interactive_script())
        out.write_text(html, encoding="utf-8")
    return out


def _build_pyvis_interactive_script() -> str:
    """pyvis HTML에 주입할 인터랙티브 JS — Ego-Network 하이라이트 + 검색 + 커뮤니티 필터 + 안정화 후 정지."""
    return """<script type="text/javascript">
(function() {
  var container = document.getElementById("mynetwork");
  container.style.position = "relative";

  // ── 1. Ego-Network 하이라이트 (클릭 → 1/2차 이웃만 강조, 나머지 반투명) ──
  network.on("click", function(params) {
    if (!params.nodes.length) {
      nodes.update(nodes.get().map(function(n) { return {id: n.id, opacity: 1}; }));
      edges.update(edges.get().map(function(e) { return {id: e.id, opacity: 1}; }));
      return;
    }
    var clicked = params.nodes[0];
    var nbrs = new Set([clicked]);
    var adj = network.getConnectedNodes(clicked);
    adj.forEach(function(n) { nbrs.add(n); });
    adj.forEach(function(n) {
      network.getConnectedNodes(n).forEach(function(n2) { nbrs.add(n2); });
    });
    nodes.update(nodes.get().map(function(n) {
      return {id: n.id, opacity: nbrs.has(n.id) ? 1 : 0.12};
    }));
    edges.update(edges.get().map(function(e) {
      return {id: e.id, opacity: (nbrs.has(e.from) && nbrs.has(e.to)) ? 1 : 0.08};
    }));
  });

  // ── 2. 노드 검색 ──
  var search = document.createElement("input");
  search.type = "text";
  search.placeholder = "🔍 노드 검색";
  search.style.cssText = "position:absolute;top:12px;left:12px;z-index:10;padding:8px 10px;width:200px;border:1px solid #ccc;border-radius:6px;font-size:13px;";
  container.appendChild(search);
  search.addEventListener("keyup", function() {
    var q = search.value.trim().toLowerCase();
    if (!q) { network.selectNodes([]); return; }
    var hits = nodes.get().filter(function(n) { return String(n.label).toLowerCase().indexOf(q) !== -1; });
    var ids = hits.map(function(n) { return n.id; });
    network.selectNodes(ids);
    if (ids.length) network.focus(ids[0], {scale: 1.3, animation: {duration: 500}});
  });

  // ── 3. 커뮤니티 필터 드롭다운 ──
  var sel = document.createElement("select");
  sel.style.cssText = "position:absolute;top:12px;right:12px;z-index:10;padding:8px;border:1px solid #ccc;border-radius:6px;font-size:13px;";
  var all = document.createElement("option");
  all.value = "all"; all.text = "전체 커뮤니티";
  sel.appendChild(all);
  var comms = {};
  nodes.get().forEach(function(n) { comms[n.comm] = true; });
  Object.keys(comms).sort().forEach(function(c) {
    var opt = document.createElement("option");
    opt.value = c; opt.text = "커뮤니티 " + c;
    sel.appendChild(opt);
  });
  container.appendChild(sel);
  sel.addEventListener("change", function() {
    var c = sel.value;
    nodes.update(nodes.get().map(function(n) {
      return {id: n.id, hidden: (c !== "all" && String(n.comm) !== c)};
    }));
    edges.update(edges.get().map(function(e) {
      var f = nodes.get(e.from), t = nodes.get(e.to);
      return {id: e.id, hidden: (c !== "all" && (String(f.comm) !== c || String(t.comm) !== c))};
    }));
  });

  // ── 4. 안정화 완료 후 물리 엔진 off (진동 방지) ──
  network.once("stabilizationIterationsDone", function() {
    network.setOptions({ physics: false });
  });
})();
</script>
</body>"""


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


def render_force_graph_3d(
    G: nx.Graph,
    node_comm: dict[str, int],
    cent: dict[str, dict[str, float]] | None = None,
    out_path: Path | None = None,
) -> Path:
    """👑 [2026-08-07] 지식그래프 3D — WebGL 3d-force-graph CDN (대용량 엣지 렌더링 성능).
    plotly scatter3d 대체. 노드 크기=PageRank, 색=커뮤니티, 엣지 굵기=weight.
    Ego-Network 하이라이트 + 검색 + 커뮤니티 필터 포함."""
    import json

    VAULT_INSIGHTS.mkdir(parents=True, exist_ok=True)
    out = out_path or (VAULT_INSIGHTS / "knowledge_graph_3d.html")

    if cent is None:
        cent = compute_centralities(G)
    pr = cent.get("pagerank", {})
    pr_max = max(pr.values()) if pr else 0.0
    palette = ["#0969da", "#cf222e", "#1a7f37", "#bf8700", "#8250df", "#1f6feb", "#a371f7"]

    nodes = []
    for n, d in G.nodes(data=True):
        c = node_comm.get(n, 0)
        nodes.append({
            "id": n, "label": n, "type": d.get("type", ""), "comm": c,
            "color": palette[c % len(palette)],
            "size": 1 + 5 * (pr.get(n, 0) / pr_max) if pr_max > 0 else 2,
            "freq": d.get("freq", 1),
        })
    links = [{"source": a, "target": b, "weight": d.get("weight", 1)}
             for a, b, d in G.edges(data=True)]

    data_json = json.dumps({"nodes": nodes, "links": links}, ensure_ascii=False)
    html = _FORCE_GRAPH_TEMPLATE.replace("__DATA_JSON__", data_json)
    out.write_text(html, encoding="utf-8")
    return out


_FORCE_GRAPH_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>지식그래프 3D (3d-force-graph)</title>
<style>
  body { margin: 0; overflow: hidden; font-family: -apple-system, sans-serif; }
  #graph { width: 100vw; height: 100vh; }
  .ctrl { position: absolute; z-index: 10; padding: 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; background: #fff; }
</style>
</head>
<body>
<div id="graph"></div>
<script src="https://unpkg.com/3d-force-graph"></script>
<script>
const data = __DATA_JSON__;
// 주의: UMD 글로벌은 ForceGraph3D (ForceGraph 아님) — 잘못 쓰면 ReferenceError로 렌더링 안 됨
const Graph = ForceGraph3D()(document.getElementById('graph'))
  .graphData(data)
  .nodeId('id')
  .nodeLabel(n => `${n.label} · ${n.type} · freq=${n.freq} · comm=${n.comm}`)
  .nodeColor(n => n.color)
  .nodeVal(n => n.size)
  .linkWidth(l => Math.sqrt(l.weight))
  .linkColor(() => 'rgba(120,120,120,0.35)')
  .linkOpacity(0.4)
  .backgroundColor('#ffffff');
// 레이아웃 후 전체 자동 프레이밍 (카메라가 그래프를 비추도록)
setTimeout(() => Graph.zoomToFit(800), 1800);

// ── 1. Ego-Network 하이라이트 (클릭 → 1/2차 이웃만 강조, 나머지 반투명) ──
Graph.onNodeClick(node => {
  if (!node) return;
  const nbrs = new Set([node.id]);
  const adj = data.links.filter(l => l.source.id === node.id || l.target.id === node.id)
    .map(l => l.source.id === node.id ? l.target.id : l.source.id);
  adj.forEach(id => nbrs.add(id));
  adj.forEach(id => {
    data.links.filter(l => l.source.id === id || l.target.id === id)
      .forEach(l => nbrs.add(l.source.id === id ? l.target.id : l.source.id));
  });
  Graph.nodeOpacity(n => nbrs.has(n.id) ? 1 : 0.12)
       .linkOpacity(l => (nbrs.has(l.source.id) && nbrs.has(l.target.id)) ? 0.8 : 0.05);
});

// ── 2. 노드 검색 ──
const search = document.createElement('input');
search.type = 'text';
search.placeholder = '🔍 노드 검색';
search.className = 'ctrl';
search.style.cssText = 'top:12px;left:12px;width:200px;';
document.body.appendChild(search);
search.addEventListener('keyup', () => {
  const q = search.value.trim().toLowerCase();
  if (!q) return;
  const hit = data.nodes.find(n => n.label.toLowerCase().includes(q));
  if (hit) Graph.centerObject(hit, 1000);
});

// ── 3. 커뮤니티 필터 드롭다운 ──
const sel = document.createElement('select');
sel.className = 'ctrl';
sel.style.cssText = 'top:12px;right:12px;';
const all = document.createElement('option');
all.value = 'all'; all.text = '전체 커뮤니티';
sel.appendChild(all);
[...new Set(data.nodes.map(n => n.comm))].sort().forEach(c => {
  const opt = document.createElement('option');
  opt.value = c; opt.text = '커뮤니티 ' + c;
  sel.appendChild(opt);
});
document.body.appendChild(sel);
sel.addEventListener('change', () => {
  const c = sel.value;
  Graph.nodeVisibility(n => c === 'all' || String(n.comm) === c);
});
</script>
</body>
</html>"""


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
    Gp, comm, cent = build_visualization_graph(G)
    print(f"정제 전: 노드 {G.number_of_nodes()} 엣지 {G.number_of_edges()} → "
          f"정제 후: 노드 {Gp.number_of_nodes()} 엣지 {Gp.number_of_edges()} "
          f"커뮤니티 {len(set(comm.values()))}")
    summarize_network(Gp, comm)
    out = render_pyvis(Gp, comm, cent)
    print("pyvis:", out)
    out3d = render_force_graph_3d(Gp, comm, cent)
    print("3D:", out3d)