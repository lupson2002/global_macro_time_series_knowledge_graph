#!/usr/bin/env python3
"""Audit SQLite-derived projections and optionally repair missing artifacts."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import queue as queue_module
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings  # noqa: E402
from src.exporter import ObsidianMDExporter  # noqa: E402
from src.lancedb_store import list_video_ids  # noqa: E402
from src.reconciliation import (  # noqa: E402
    audit_storage,
    create_backup,
    plan_as_dict,
    repair_missing,
    upsert_schema_vector,
)


def _path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit SQLite, Obsidian and LanceDB consistency. Default is read-only."
    )
    parser.add_argument("--db-path", default=str(settings.storage.sqlite_path))
    parser.add_argument("--vault-dir", default=str(settings.storage.obsidian_vault))
    parser.add_argument("--lancedb-dir", default=str(settings.storage.lancedb_dir))
    parser.add_argument("--backup-root", default="backups/reconciliation")
    parser.add_argument("--vector-timeout", type=float, default=15.0)
    parser.add_argument(
        "--markdown-only",
        action="store_true",
        help="Audit/repair Markdown without opening LanceDB",
    )
    parser.add_argument("--apply", action="store_true", help="Create missing projections")
    parser.add_argument(
        "--yes", action="store_true", help="Required confirmation for --apply"
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def _vector_id_worker(lancedb_dir: str, queue) -> None:
    try:
        queue.put(("ok", list(list_video_ids(Path(lancedb_dir)))))
    except Exception as exc:  # noqa: BLE001
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def read_vector_ids(lancedb_dir: Path, timeout: float) -> frozenset[str]:
    """Read LanceDB IDs in a killable process so native locks cannot hang the audit."""
    if not lancedb_dir.exists():
        return frozenset()
    context = mp.get_context("spawn")
    queue = context.Queue()
    process = context.Process(
        target=_vector_id_worker, args=(str(lancedb_dir), queue), daemon=True
    )
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise TimeoutError(f"LanceDB ID scan exceeded {timeout:g}s")
    try:
        status, payload = queue.get(timeout=1)
    except queue_module.Empty as exc:
        raise RuntimeError(
            f"LanceDB ID worker exited with code {process.exitcode}"
        ) from exc
    queue.close()
    if status != "ok":
        raise RuntimeError(payload)
    return frozenset(payload)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    counts = payload["counts"]
    drift = payload["drift"]
    print(
        f"SQLite={counts['sqlite']} Markdown={counts['markdown']} "
        f"LanceDB={counts['vectors'] if counts['vectors'] is not None else 'unavailable'}"
    )
    for key, values in drift.items():
        print(f"{key}: {len(values)}" + (f" ({', '.join(values)})" if values else ""))
    if payload.get("backup"):
        print(f"backup: {payload['backup']}")
    if payload.get("vector_audit_error"):
        print(f"vector_audit_error: {payload['vector_audit_error']}")
    if payload.get("vector_audit"):
        print(f"vector_audit: {payload['vector_audit']}")
    if payload.get("repair"):
        repair = payload["repair"]
        print(
            f"repaired: markdown={len(repair['markdown_repaired'])} "
            f"vectors={len(repair['vectors_repaired'])} failures={len(repair['failures'])}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = _path(args.db_path)
    vault_path = _path(args.vault_dir)
    lancedb_dir = _path(args.lancedb_dir)
    backup_root = _path(args.backup_root)
    if args.vector_timeout <= 0:
        print("--vector-timeout must be greater than zero", file=sys.stderr)
        return 64
    if args.apply and not args.yes:
        print("--apply requires --yes; no files were changed", file=sys.stderr)
        return 64
    vector_error = ""
    if args.markdown_only:
        vector_ids = None
    else:
        try:
            vector_ids = read_vector_ids(lancedb_dir, args.vector_timeout)
        except Exception as exc:  # noqa: BLE001
            vector_ids = None
            vector_error = f"{type(exc).__name__}: {exc}"
    try:
        snapshot, plan, schemas = audit_storage(db_path, vault_path, vector_ids)
    except Exception as exc:  # noqa: BLE001
        print(f"audit could not complete: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = plan_as_dict(snapshot, plan)
    if args.markdown_only:
        payload["vector_audit"] = "skipped_by_request"
    if vector_error:
        payload["vector_audit_error"] = vector_error
        if args.apply:
            _emit(payload, args.json)
            print("repair refused because vector audit is incomplete", file=sys.stderr)
            return 1
    if not args.apply or plan.repairable_count == 0:
        _emit(payload, args.json)
        if vector_error:
            return 1
        return 2 if plan.has_drift else 0

    try:
        backup_path = create_backup(db_path, lancedb_dir, backup_root)
        repair = repair_missing(
            plan,
            schemas,
            markdown_exporter=ObsidianMDExporter(str(vault_path)),
            vector_upsert=lambda schema: upsert_schema_vector(schema, lancedb_dir),
        )
        payload["backup"] = str(backup_path)
        payload["repair"] = {
            "markdown_repaired": list(repair.markdown_repaired),
            "vectors_repaired": list(repair.vectors_repaired),
            "failures": list(repair.failures),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"repair could not complete: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _emit(payload, args.json)
    if not repair.succeeded:
        return 1
    remaining_vector_ids = (
        None
        if args.markdown_only
        else read_vector_ids(lancedb_dir, args.vector_timeout)
    )
    _, remaining, _ = audit_storage(db_path, vault_path, remaining_vector_ids)
    return 2 if remaining.has_drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
