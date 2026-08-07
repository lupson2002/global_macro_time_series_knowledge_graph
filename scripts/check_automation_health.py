#!/usr/bin/env python3
"""Read-only watchdog for the structured automation journal."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.event_summary import read_journal, summarize_runs  # noqa: E402


def evaluate_health(
    summaries: list[dict[str, object]], *, now: datetime, stale_hours: float
) -> dict[str, object]:
    latest: dict[str, dict[str, object]] = {}
    threshold = now - timedelta(hours=stale_hours)
    for summary in summaries:
        subject = str(summary.get("report") or summary.get("mode") or "unknown")
        latest.setdefault(subject, summary)
    stale_runs: list[str] = []
    for summary in latest.values():
        if summary.get("status") == "running" and summary.get("started_at"):
            started = datetime.fromisoformat(str(summary["started_at"]))
            if started.astimezone(timezone.utc) < threshold:
                stale_runs.append(str(summary["run_id"]))
    failed_subjects = sorted(
        subject for subject, summary in latest.items()
        if summary.get("status") == "failed"
    )
    return {
        "healthy": not stale_runs and not failed_subjects,
        "latest_status": {
            subject: summary.get("status") for subject, summary in sorted(latest.items())
        },
        "stale_runs": stale_runs,
        "failed_subjects": failed_subjects,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check scheduled automation journal health")
    parser.add_argument(
        "--event-log",
        default=os.environ.get("PIPELINE_EVENT_LOG", "logs/pipeline-events.jsonl"),
    )
    parser.add_argument("--stale-hours", type=float, default=8.0)
    args = parser.parse_args(argv)
    path = PROJECT_ROOT / args.event_log
    try:
        result = read_journal(path)
    except FileNotFoundError:
        print(json.dumps({"healthy": True, "status": "awaiting_first_event"}))
        return 0
    health = evaluate_health(
        summarize_runs(result.events), now=datetime.now(timezone.utc),
        stale_hours=args.stale_hours,
    )
    health["malformed_lines"] = result.malformed_lines
    print(json.dumps(health, ensure_ascii=False, sort_keys=True))
    return 0 if health["healthy"] and not result.malformed_lines else 2


if __name__ == "__main__":
    raise SystemExit(main())
