"""크로스 집계 매트릭스 — 자산/테마/채널별 평균 심리 + 분산 + contrarian 비율.

정규화된 node_value/channel 기준 GROUP BY. pandas DataFrame → 마크다운 표 + CSV.
의견 분기(STDDEV 상위) 자산 = 잠재 contrarian 기회.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from .normalize import normalize_node, normalize_channel
from .timebox import valid_time_box_values

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "macro_knowledge.db"
MIN_N = 5  # 통계 유의성 최소 샘플


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _timebox_clause(use_timebox: bool) -> tuple[str, list]:
    """time_box 유효기간 필터.
      - time_box 유효(미래/현재 전망) 포함
      - time_box 만료 제외
      - time_box 빈값 → broadcast_date 기준 최근 90일만 포함
    use_timebox=False 시 전체.
    """
    if not use_timebox:
        return "", []
    valid = valid_time_box_values()
    # 빈값 분기: broadcast_date >= date('now','-90 days')
    empty_clause = "(r.time_box IS NULL OR r.time_box = '') AND r.broadcast_date >= date('now','-90 days')"
    if not valid:
        return f" AND ({empty_clause}) ", []
    placeholders = ",".join("?" * len(valid))
    return f" AND (r.time_box IN ({placeholders}) OR ({empty_clause})) ", list(valid)


def _lookback_clause(lookback_days: int | None) -> tuple[str, list]:
    """broadcast_date 기준 최근 N일 필터 SQL 절. None 시 전체."""
    if lookback_days is None:
        return "", []
    return (
        " AND r.broadcast_date >= date('now', ?) ",
        [f"-{lookback_days} days"],
    )


def _matrix_by_node_type(
    node_type: str,
    use_timebox: bool = True,
    lookback_days: int | None = None,
) -> pd.DataFrame:
    """node_type(asset_class/macro_theme/ticker) 기준 집계. 정규화 적용. time_box 유효기간 + 옵션 lookback 필터."""
    conn = _connect()
    try:
        tb_clause, tb_params = _timebox_clause(use_timebox)
        lb_clause, lb_params = _lookback_clause(lookback_days)
        rows = conn.execute(
            f"""SELECT n.node_value, q.bull_bear_score, q.conviction_score, q.contrarian_flag
               FROM nodes n
               JOIN quant_signals q ON n.video_id = q.video_id
               JOIN reports r ON n.video_id = r.video_id
               WHERE n.node_type = ? AND q.bull_bear_score IS NOT NULL{tb_clause}{lb_clause}""",
            [node_type, *tb_params, *lb_params],
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df["norm"] = df["node_value"].apply(lambda v: normalize_node(v, node_type))
    g = df.groupby("norm").agg(
        n=("bull_bear_score", "size"),
        avg_bull_bear=("bull_bear_score", "mean"),
        stddev_bull_bear=("bull_bear_score", "std"),
        avg_conviction=("conviction_score", "mean"),
        contrarian_pct=("contrarian_flag", "mean"),
    ).reset_index()
    g = g[g["n"] >= MIN_N].sort_values("avg_bull_bear", ascending=False)
    g["contrarian_pct"] = (g["contrarian_pct"] * 100).round(1)
    g[["avg_bull_bear", "stddev_bull_bear", "avg_conviction"]] = g[
        ["avg_bull_bear", "stddev_bull_bear", "avg_conviction"]
    ].round(2)
    return g.rename(columns={"norm": node_type})


def asset_consensus_matrix(use_timebox: bool = True, lookback_days: int | None = None) -> pd.DataFrame:
    return _matrix_by_node_type("asset_class", use_timebox, lookback_days)


def theme_consensus_matrix(use_timebox: bool = True, lookback_days: int | None = None) -> pd.DataFrame:
    return _matrix_by_node_type("macro_theme", use_timebox, lookback_days)


def ticker_consensus_matrix(use_timebox: bool = True, lookback_days: int | None = None) -> pd.DataFrame:
    return _matrix_by_node_type("ticker", use_timebox, lookback_days)


def channel_matrix(use_timebox: bool = True, lookback_days: int | None = None) -> pd.DataFrame:
    conn = _connect()
    try:
        tb_clause, tb_params = _timebox_clause(use_timebox)
        lb_clause, lb_params = _lookback_clause(lookback_days)
        rows = conn.execute(
            f"""SELECT r.source_channel, q.bull_bear_score, q.conviction_score, q.contrarian_flag
               FROM reports r JOIN quant_signals q ON r.video_id = q.video_id
               WHERE q.bull_bear_score IS NOT NULL{tb_clause}{lb_clause}""",
            [*tb_params, *lb_params],
        ).fetchall()
    finally:
        conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["channel"] = df["source_channel"].apply(normalize_channel)
    g = df.groupby("channel").agg(
        n=("bull_bear_score", "size"),
        avg_bull_bear=("bull_bear_score", "mean"),
        stddev_bull_bear=("bull_bear_score", "std"),
        avg_conviction=("conviction_score", "mean"),
        contrarian_pct=("contrarian_flag", "mean"),
    ).reset_index()
    g = g[g["n"] >= MIN_N].sort_values("avg_bull_bear", ascending=False)
    g["contrarian_pct"] = (g["contrarian_pct"] * 100).round(1)
    g[["avg_bull_bear", "stddev_bull_bear", "avg_conviction"]] = g[
        ["avg_bull_bear", "stddev_bull_bear", "avg_conviction"]
    ].round(2)
    return g


def divergence_opportunities(top_n: int = 10, use_timebox: bool = True, lookback_days: int | None = None) -> pd.DataFrame:
    """stddev 내림차순 상위 자산 = 의견 분기 = 잠재 contrarian 기회."""
    df = asset_consensus_matrix(use_timebox=use_timebox, lookback_days=lookback_days)
    if df.empty:
        return df
    return df.sort_values("stddev_bull_bear", ascending=False).head(top_n)


def df_to_markdown(df: pd.DataFrame, title: str = "") -> str:
    """DataFrame → 마크다운 표."""
    lines = []
    if title:
        lines.append(f"### {title}")
        lines.append("")
    if df.empty:
        lines.append("(데이터 없음)")
        lines.append("")
        return "\n".join(lines)
    lines.append(df.to_markdown(index=False))
    lines.append("")
    return "\n".join(lines)


def build_all_matrices(use_timebox: bool = True, lookback_days: int | None = None) -> dict[str, pd.DataFrame]:
    return {
        "asset": asset_consensus_matrix(use_timebox=use_timebox, lookback_days=lookback_days),
        "theme": theme_consensus_matrix(use_timebox=use_timebox, lookback_days=lookback_days),
        "ticker": ticker_consensus_matrix(use_timebox=use_timebox, lookback_days=lookback_days),
        "channel": channel_matrix(use_timebox=use_timebox, lookback_days=lookback_days),
        "divergence": divergence_opportunities(use_timebox=use_timebox, lookback_days=lookback_days),
    }


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parent.parent.parent / "reports" / "insights"
    out_dir.mkdir(parents=True, exist_ok=True)
    mats = build_all_matrices()
    for k, df in mats.items():
        print(f"\n=== {k} (n={len(df)}) ===")
        print(df.head(15).to_string(index=False))
        df.to_csv(out_dir / f"matrix_{k}.csv", index=False)