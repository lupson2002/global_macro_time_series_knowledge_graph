"""Read-only summaries for structured pipeline JSONL journals."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JournalReadResult:
    events: tuple[dict[str, object], ...]
    malformed_lines: int = 0


def read_journal(path: Path) -> JournalReadResult:
    """Read valid schema-v1 events without modifying the journal."""
    events: list[dict[str, object]] = []
    malformed = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not _is_event(event):
                malformed += 1
                continue
            events.append(event)
    return JournalReadResult(tuple(events), malformed)


def _is_event(value: object) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    return all(isinstance(value.get(key), str) for key in ("event", "status", "run_id", "timestamp"))


def summarize_runs(
    events: Iterable[Mapping[str, object]], *, limit: int | None = None
) -> list[dict[str, object]]:
    """Collapse lifecycle events into newest-first per-run summaries."""
    runs: dict[str, dict[str, object]] = {}
    for event in events:
        run_id = str(event["run_id"])
        summary = runs.setdefault(
            run_id,
            {
                "run_id": run_id,
                "started_at": None,
                "finished_at": None,
                "status": "running",
                "stage": None,
                "mode": None,
                "report": None,
                "videos_started": 0,
                "videos_finished": 0,
                "processed": 0,
                "skipped": 0,
                "failed": 0,
            },
        )
        kind = event["event"]
        timestamp = event["timestamp"]
        details = event.get("details")
        details = details if isinstance(details, Mapping) else {}
        if kind == "run.started":
            summary["started_at"] = timestamp
            summary["mode"] = details.get("mode")
        elif kind == "run.finished":
            summary["finished_at"] = timestamp
            summary["status"] = event["status"]
            summary["stage"] = event.get("stage")
            for key in ("processed", "skipped", "failed"):
                if isinstance(details.get(key), int):
                    summary[key] = details[key]
        elif kind == "report.started":
            summary["report"] = event.get("report")
        elif kind == "video.started":
            summary["videos_started"] = int(summary["videos_started"]) + 1
        elif kind == "video.finished":
            summary["videos_finished"] = int(summary["videos_finished"]) + 1

    ordered = sorted(
        runs.values(),
        key=lambda item: str(item["finished_at"] or item["started_at"] or ""),
        reverse=True,
    )
    return ordered[:limit] if limit is not None else ordered


def format_run_summary(summary: Mapping[str, object]) -> str:
    """Render one compact operational line."""
    subject = summary.get("report") or summary.get("mode") or "unknown"
    timestamp = summary.get("finished_at") or summary.get("started_at") or "unknown-time"
    videos = f" videos={summary['videos_finished']}/{summary['videos_started']}"
    counts = (
        f" processed={summary['processed']} skipped={summary['skipped']}"
        f" failed={summary['failed']}"
    )
    return f"{timestamp} {summary['status']} {subject} run={summary['run_id']}{videos}{counts}"
