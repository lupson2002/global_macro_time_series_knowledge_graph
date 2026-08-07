# -*- coding: utf-8 -*-
"""
weekly_analytics.py — [주간] 4대 고급 매크로 분석 엔진 (신규 독립 모듈)
========================================================================
최근 7일 vs 30일 베이스라인으로 4가지 핵심 지표를 정형화 추출:
  1. Narrative Velocity  — 7일 언급 / 30일 일평균 → 주도 내러티브 스파이크 TOP5
  2. Friction Index      — stddev(bull_bear) × mean(conviction) → 변곡/불확실성 자산 TOP3
  3. Causal Centrality   — causal_chain JSON → networkx DiGraph → PageRank 근본 원인
  4. Guru Threshold Range— tracking_indicators JSON → 지표별 상승/하락 임계 참조표

Usage:
    .venv/bin/python scripts/insights/weekly_analytics.py --days 7
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

# 👑 [2026-08-06 L4] 서버=UTC vs broadcast_date=KST 1일 시차 보정 — UTC 00:00~09:00
# 사이(KST 09:00~18:00)에 date.today()/date('now') 가 하루 느림.
import datetime as _dt
KST_TODAY = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=9)).date()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "macro_knowledge.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _jlist(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# 1) Narrative Velocity — 주도 내러티브 스파이크 TOP5
# ---------------------------------------------------------------------------
def narrative_velocity(days: int = 7, baseline_days: int = 30, top_k: int = 5, min_7d: int = 2) -> list[dict]:
    """Velocity = 7일 언급수 / (30일 일평균 언급수). 최근 7일 급가속 테마 스파이크.
    min_7d: 노이즈 방지 최소 7일 언급수."""
    conn = _connect()
    try:
        # 30일 기준 일자별 테마/티커 언급
        rows30 = conn.execute(
            "SELECT r.broadcast_date, n.node_value FROM nodes n "
            "JOIN reports r ON n.video_id = r.video_id "
            "WHERE r.broadcast_date >= date('now', '+9 hours', ?) AND n.node_type IN ('macro_theme','ticker')",
            (f"-{baseline_days} days",),
        ).fetchall()
    finally:
        conn.close()

    node_counts30 = Counter()
    node_counts7 = Counter()
    for r in rows30:
        v = (r["node_value"] or "").strip("[]").strip()
        if not v:
            continue
        node_counts30[v] += 1
        if r["broadcast_date"] >= (KST_TODAY - _dt.timedelta(days=days)).isoformat():
            node_counts7[v] += 1

    out = []
    for node, c7 in node_counts7.items():
        if c7 < min_7d:
            continue
        c30 = node_counts30[node]
        daily_avg30 = c30 / baseline_days
        if daily_avg30 <= 0:
            continue
        out.append({"node": node, "count_7d": c7, "count_30d": c30,
                    "daily_avg_30d": round(daily_avg30, 2), "velocity": round(c7 / daily_avg30, 2)})
    return sorted(out, key=lambda x: -x["velocity"])[:top_k]


# ---------------------------------------------------------------------------
# 2) Friction Index — 변곡/불확실성 자산 TOP3
# ---------------------------------------------------------------------------
def friction_index(top_k: int = 3, min_n: int = 5, use_timebox: bool = True) -> list[dict]:
    """Friction = stddev(bull_bear) × mean(conviction). 확신 높은데 의견 대립 심한 자산."""
    from src.insights.cross_matrix import asset_consensus_matrix
    df = asset_consensus_matrix(use_timebox=use_timebox)
    if df is None or df.empty:
        return []
    df = df[df["n"] >= min_n].copy()
    if df.empty:
        return []
    df["friction"] = (df["stddev_bull_bear"] * df["avg_conviction"]).round(2)
    df = df.sort_values("friction", ascending=False).head(top_k)
    return [{"asset": r["asset_class"], "stddev": r["stddev_bull_bear"],
             "conviction": r["avg_conviction"], "friction": r["friction"], "n": int(r["n"])}
            for _, r in df.iterrows()]


# ---------------------------------------------------------------------------
# 3) Causal Centrality — 근본 원인 노드 (PageRank)
# ---------------------------------------------------------------------------
def causal_centrality(top_k: int = 10, lookback_days: int = 90) -> list[dict]:
    """reports.causal_chain JSON → networkx DiGraph (원인→결과 엣지) → PageRank.
    전체 매크로 위기/상승의 최상위 근본 원인(Root Bottleneck) 노드 추출."""
    import networkx as nx

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT causal_chain FROM reports WHERE broadcast_date >= date('now', ?)",
            (f"-{lookback_days} days",),
        ).fetchall()
    finally:
        conn.close()

    G = nx.DiGraph()
    for r in rows:
        chain = [str(x).strip() for x in _jlist(r["causal_chain"]) if str(x).strip()]
        for a, b in zip(chain, chain[1:]):
            if G.has_edge(a, b):
                G[a][b]["weight"] += 1
            else:
                G.add_edge(a, b, weight=1)

    if G.number_of_nodes() == 0:
        return []

    try:
        pr = nx.pagerank(G, weight="weight")
    except Exception:
        pr = nx.pagerank(G)
    ranked = sorted(pr.items(), key=lambda x: -x[1])[:top_k]
    return [{"node": n, "pagerank": round(float(v), 4)} for n, v in ranked]


# ---------------------------------------------------------------------------
# 4) Guru Threshold Range — 구루 임계값 컨센서스 참조표
# ---------------------------------------------------------------------------
def guru_threshold_range(lookback_days: int = 90) -> dict[str, dict]:
    """tracking_indicators JSON → 지표별 {bull_conditions, bear_thresholds} 그룹핑.
    implication 텍스트로 상승/하락 방향 분류(외부 시세 API 없이 구루 제시값만)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT tracking_indicators FROM reports WHERE broadcast_date >= date('now', ?)",
            (f"-{lookback_days} days",),
        ).fetchall()
    finally:
        conn.close()

    by_metric: dict[str, dict] = defaultdict(lambda: {"bull": [], "bear": [], "neutral": []})
    _BULL = ("bull", "confirm", "upside", "support", "매수", "상승", "긍정", "호재")
    _BEAR = ("bear", "risk", "break", "fail", "매도", "하락", "부정", "악재", "위험")

    for r in rows:
        for ind in _jlist(r["tracking_indicators"]):
            if not isinstance(ind, dict):
                continue
            metric = str(ind.get("metric") or "").strip()
            thr = str(ind.get("threshold") or "").strip()
            impl = str(ind.get("implication") or "").strip()
            if not metric or not thr:
                continue
            key = metric[:40]
            bucket = "neutral"
            if any(k in impl.lower() for k in _BULL):
                bucket = "bull"
            elif any(k in impl.lower() for k in _BEAR):
                bucket = "bear"
            entry = thr + (f" → {impl}" if impl else "")
            if entry not in by_metric[key][bucket]:
                by_metric[key][bucket].append(entry)

    return {k: {"bull": v["bull"], "bear": v["bear"], "neutral": v["neutral"]}
            for k, v in sorted(by_metric.items())}


def threshold_reference_table(top_n: int = 12) -> str:
    """구루 임계값 컨센서스 참조표 — 마크다운 표."""
    data = guru_threshold_range()
    if not data:
        return "*(tracking_indicators 데이터 없음)*"
    lines = ["### 🎚️ 구루 임계값 컨센서스 참조표", "",
             "| 지표 | 상승 성공 조건 | 하락 파탄 임계값 |", "|------|--------------|----------------|"]
    for metric, buckets in list(data.items())[:top_n]:
        bull = "; ".join(buckets["bull"]) if buckets["bull"] else "-"
        bear = "; ".join(buckets["bear"]) if buckets["bear"] else "-"
        lines.append(f"| {metric} | {bull.replace('|','\\|')} | {bear.replace('|','\\|')} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="주간 4대 고급 매크로 분석")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--which", choices=["velocity", "friction", "causal", "threshold", "all"], default="all")
    args = ap.parse_args()

    if args.which in ("all", "velocity"):
        print("=== 1) Narrative Velocity (7d vs 30d 스파이크) TOP5 ===")
        for x in narrative_velocity(days=args.days):
            print(f"  {x['node']}: 7d {x['count_7d']} / 30d일평균 {x['daily_avg_30d']} → velocity {x['velocity']}")
    if args.which in ("all", "friction"):
        print("=== 2) Friction Index TOP3 ===")
        for x in friction_index():
            print(f"  {x['asset']}: std {x['stddev']} × conv {x['conviction']} = friction {x['friction']} (n={x['n']})")
    if args.which in ("all", "causal"):
        print("=== 3) Causal Centrality (PageRank 근본원인) TOP10 ===")
        for x in causal_centrality():
            print(f"  {x['pagerank']:.4f}  {x['node']}")
    if args.which in ("all", "threshold"):
        print("=== 4) Guru Threshold Range ===")
        print(threshold_reference_table())
