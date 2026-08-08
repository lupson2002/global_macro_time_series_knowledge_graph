"""Deterministic, source-balanced signals for weekly reports and change alerts."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.insights.normalize import normalize_channel, normalize_node, normalize_speaker


def _kst_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def _window_rows(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.video_id, r.source_channel, r.speaker_name, r.broadcast_date,
                  q.bull_bear_score, q.conviction_score, q.contrarian_flag,
                  n.node_value
           FROM reports r
           JOIN quant_signals q ON q.video_id = r.video_id
           LEFT JOIN nodes n ON n.video_id = r.video_id AND n.node_type = 'asset_class'
           WHERE r.broadcast_date >= ? AND r.broadcast_date <= ?
             AND q.bull_bear_score BETWEEN 1 AND 10""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


def _balanced_summary(rows: list[sqlite3.Row], asset: str | None = None) -> dict:
    """Average speakers inside channels, then channels equally.

    This prevents one prolific speaker or channel from becoming the market consensus.
    Duplicate asset nodes inside a video are removed before aggregation.
    """
    observations: dict[tuple[str, str, str], tuple[float, float, bool]] = {}
    for row in rows:
        row_asset = normalize_node(row["node_value"], "asset_class") if row["node_value"] else None
        if asset is not None and row_asset != asset:
            continue
        channel = normalize_channel(row["source_channel"] or "Unknown") or "Unknown"
        speaker = normalize_speaker(row["speaker_name"] or "Unknown")
        key = (str(row["video_id"]), channel, speaker)
        observations[key] = (
            float(row["bull_bear_score"]),
            float(row["conviction_score"] or 0),
            bool(row["contrarian_flag"]),
        )
    if not observations:
        return {
            "stance": None, "dispersion": None, "agreement": None,
            "tail_risk_ratio": 0.0, "views": 0, "speakers": 0, "channels": 0,
        }

    by_speaker: dict[tuple[str, str], list[tuple[float, float, bool]]] = defaultdict(list)
    for (_, channel, speaker), values in observations.items():
        by_speaker[(channel, speaker)].append(values)

    speaker_scores: dict[str, list[tuple[float, float, bool]]] = defaultdict(list)
    for (channel, _speaker), values in by_speaker.items():
        score = sum(v[0] for v in values) / len(values)
        conviction = sum(v[1] for v in values) / len(values)
        tail = any(v[0] <= 4 and v[1] >= 8 for v in values)
        speaker_scores[channel].append((score, conviction, tail))

    channel_scores = []
    tail_channels = 0
    for values in speaker_scores.values():
        # sqrt conviction retains information without allowing a single emphatic view to dominate.
        weights = [math.sqrt(max(1.0, v[1])) for v in values]
        score = sum(v[0] * w for v, w in zip(values, weights)) / sum(weights)
        channel_scores.append(score)
        if any(v[2] for v in values):
            tail_channels += 1

    mean = sum(channel_scores) / len(channel_scores)
    variance = sum((value - mean) ** 2 for value in channel_scores) / len(channel_scores)
    std = math.sqrt(variance)
    return {
        "stance": round((mean - 1.0) / 9.0 * 100),
        "dispersion": round(std, 2),
        "agreement": round(max(0.0, min(100.0, 100.0 * (1.0 - std / 4.5)))),
        "tail_risk_ratio": round(tail_channels / len(channel_scores), 3),
        "views": len(observations),
        "speakers": len(by_speaker),
        "channels": len(channel_scores),
    }


def _theme_velocity(conn: sqlite3.Connection, today: date, top_k: int = 10) -> list[dict]:
    current_start = today - timedelta(days=6)
    baseline_start = today - timedelta(days=29)
    baseline_end = current_start - timedelta(days=1)
    rows = conn.execute(
        """SELECT r.video_id, r.broadcast_date, n.node_type, n.node_value
           FROM nodes n JOIN reports r ON r.video_id = n.video_id
           WHERE n.node_type IN ('macro_theme','ticker')
             AND r.broadcast_date >= ? AND r.broadcast_date <= ?""",
        (baseline_start.isoformat(), today.isoformat()),
    ).fetchall()
    current: Counter[str] = Counter()
    baseline: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        node = normalize_node(row["node_value"], row["node_type"])
        key = (str(row["video_id"]), node)
        if not node or key in seen:
            continue
        seen.add(key)
        broadcast = date.fromisoformat(str(row["broadcast_date"])[:10])
        if broadcast >= current_start:
            current[node] += 1
        elif broadcast <= baseline_end:
            baseline[node] += 1
    result = []
    for node, count_7d in current.items():
        if count_7d < 3:
            continue
        prior_count = baseline[node]
        current_rate = count_7d / 7.0
        prior_rate = prior_count / 23.0
        velocity = current_rate / prior_rate if prior_rate else None
        result.append({
            "node": node, "count_7d": count_7d, "count_prior_23d": prior_count,
            "velocity": round(velocity, 2) if velocity is not None else None,
            "new": prior_count == 0,
        })
    return sorted(
        result,
        key=lambda item: (item["new"], item["velocity"] or 0, item["count_7d"]),
        reverse=True,
    )[:top_k]


def build_signal_snapshot(db_path: str | Path, *, today: date | None = None) -> dict:
    today = today or _kst_today()
    current_start = today - timedelta(days=6)
    previous_start = today - timedelta(days=13)
    previous_end = today - timedelta(days=7)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        current_rows = _window_rows(conn, current_start, today)
        previous_rows = _window_rows(conn, previous_start, previous_end)
        assets = sorted({
            normalize_node(row["node_value"], "asset_class")
            for row in current_rows if row["node_value"]
        })
        asset_rows = []
        for asset in assets:
            current = _balanced_summary(current_rows, asset)
            previous = _balanced_summary(previous_rows, asset)
            if current["speakers"] < 3:
                continue
            delta = None
            if current["stance"] is not None and previous["stance"] is not None:
                delta = current["stance"] - previous["stance"]
            asset_rows.append({"asset": asset, **current, "previous_stance": previous["stance"], "delta": delta})
        asset_rows.sort(key=lambda item: (abs(item["delta"] or 0), item["speakers"]), reverse=True)
        themes = _theme_velocity(conn, today)

    overall = _balanced_summary(current_rows)
    previous_overall = _balanced_summary(previous_rows)
    overall["previous_stance"] = previous_overall["stance"]
    overall["delta"] = (
        overall["stance"] - previous_overall["stance"]
        if overall["stance"] is not None and previous_overall["stance"] is not None else None
    )
    return {
        "as_of": today.isoformat(),
        "windows": {
            "current": [current_start.isoformat(), today.isoformat()],
            "previous": [previous_start.isoformat(), previous_end.isoformat()],
        },
        "overall": overall,
        "assets": asset_rows,
        "narrative_velocity": themes,
    }


def material_changes(current: dict, baseline: dict) -> list[dict]:
    """Return bounded alert facts; no LLM interpretation is used."""
    changes: list[dict] = []
    cur_overall = current.get("overall", {})
    base_overall = baseline.get("overall", {})
    if cur_overall.get("stance") is not None and base_overall.get("stance") is not None:
        delta = cur_overall["stance"] - base_overall["stance"]
        if abs(delta) >= 10:
            changes.append({"kind": "market_stance", "key": "overall", "delta": delta,
                            "current": cur_overall["stance"], "baseline": base_overall["stance"]})
    tail_delta = float(cur_overall.get("tail_risk_ratio") or 0) - float(base_overall.get("tail_risk_ratio") or 0)
    if tail_delta >= 0.10:
        changes.append({"kind": "tail_risk", "key": "overall", "delta": round(tail_delta, 3),
                        "current": cur_overall.get("tail_risk_ratio", 0)})

    base_assets = {item["asset"]: item for item in baseline.get("assets", [])}
    for item in current.get("assets", []):
        old = base_assets.get(item["asset"])
        if not old or item.get("stance") is None or old.get("stance") is None:
            continue
        delta = item["stance"] - old["stance"]
        if abs(delta) >= 12 and item.get("speakers", 0) >= 3 and item.get("channels", 0) >= 2:
            changes.append({"kind": "asset_stance", "key": item["asset"], "delta": delta,
                            "current": item["stance"], "baseline": old["stance"]})

    for item in current.get("narrative_velocity", []):
        if item.get("new") and item.get("count_7d", 0) >= 5:
            changes.append({"kind": "new_narrative", "key": item["node"],
                            "count_7d": item["count_7d"]})
        elif (item.get("velocity") or 0) >= 2.0 and item.get("count_7d", 0) >= 5:
            changes.append({"kind": "narrative_acceleration", "key": item["node"],
                            "velocity": item["velocity"], "count_7d": item["count_7d"]})
    return changes


def changes_fingerprint(changes: list[dict]) -> str:
    stable = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def load_snapshot(path: str | Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_snapshot(path: str | Path, snapshot: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
