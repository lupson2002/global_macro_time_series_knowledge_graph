#!/usr/bin/env python3
"""Summarize the append-only pipeline event journal."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.event_summary import format_run_summary, read_journal, summarize_runs  # noqa: E402


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    default_path = os.environ.get("PIPELINE_EVENT_LOG", "logs/pipeline-events.jsonl")
    parser = argparse.ArgumentParser(description="Read-only pipeline event journal summary")
    parser.add_argument("--event-log", default=default_path, help="JSONL journal path")
    parser.add_argument("--limit", type=_positive_int, default=20, help="Newest runs to show")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = PROJECT_ROOT / args.event_log
    try:
        result = read_journal(path)
    except FileNotFoundError:
        print(f"Journal not found: {path}", file=sys.stderr)
        return 1
    summaries = summarize_runs(result.events, limit=args.limit)
    if args.json:
        print(json.dumps(
            {"runs": summaries, "malformed_lines": result.malformed_lines},
            ensure_ascii=False, indent=2,
        ))
    else:
        for summary in summaries:
            print(format_run_summary(summary))
        print(f"runs={len(summaries)} malformed_lines={result.malformed_lines}")
    return 2 if result.malformed_lines else 0


if __name__ == "__main__":
    raise SystemExit(main())
