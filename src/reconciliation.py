"""Audit and safely repair SQLite-derived Obsidian and LanceDB projections."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from src.exporter import ObsidianMDExporter, _load_db_report_as_schema


_MARKDOWN_ID_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_([A-Za-z0-9_-]{11})$")


@dataclass(frozen=True)
class StorageSnapshot:
    sqlite_ids: frozenset[str]
    markdown_ids: frozenset[str]
    vector_ids: frozenset[str] | None


@dataclass(frozen=True)
class ReconciliationPlan:
    missing_markdown: tuple[str, ...]
    missing_vectors: tuple[str, ...]
    orphan_markdown: tuple[str, ...]
    orphan_vectors: tuple[str, ...]

    @property
    def has_drift(self) -> bool:
        return any(asdict(self).values())

    @property
    def repairable_count(self) -> int:
        return len(self.missing_markdown) + len(self.missing_vectors)


@dataclass(frozen=True)
class RepairResult:
    markdown_repaired: tuple[str, ...]
    vectors_repaired: tuple[str, ...]
    failures: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        return not self.failures


def load_sqlite_schemas(db_path: Path) -> dict[str, dict]:
    """Load source-of-truth schemas keyed by video ID without modifying SQLite."""
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    return {
        schema["metadata"]["video_id"]: schema
        for schema in _load_db_report_as_schema(str(db_path))
    }


def scan_markdown_ids(vault_path: Path) -> frozenset[str]:
    """Extract report video IDs from known Markdown filename convention."""
    if not vault_path.exists():
        return frozenset()
    ids: set[str] = set()
    for markdown_path in vault_path.rglob("*.md"):
        match = _MARKDOWN_ID_RE.search(markdown_path.stem)
        if match:
            ids.add(match.group(2))
    return frozenset(ids)


def build_plan(snapshot: StorageSnapshot) -> ReconciliationPlan:
    source = snapshot.sqlite_ids
    vector_ids = snapshot.vector_ids
    return ReconciliationPlan(
        missing_markdown=tuple(sorted(source - snapshot.markdown_ids)),
        missing_vectors=tuple(sorted(source - vector_ids)) if vector_ids is not None else (),
        orphan_markdown=tuple(sorted(snapshot.markdown_ids - source)),
        orphan_vectors=tuple(sorted(vector_ids - source)) if vector_ids is not None else (),
    )


def audit_storage(
    db_path: Path,
    vault_path: Path,
    vector_ids: Iterable[str] | None,
) -> tuple[StorageSnapshot, ReconciliationPlan, dict[str, dict]]:
    schemas = load_sqlite_schemas(db_path)
    snapshot = StorageSnapshot(
        sqlite_ids=frozenset(schemas),
        markdown_ids=scan_markdown_ids(vault_path),
        vector_ids=frozenset(vector_ids) if vector_ids is not None else None,
    )
    return snapshot, build_plan(snapshot), schemas


def upsert_schema_vector(schema: dict, lancedb_dir: Path | None = None) -> bool:
    """Project one rehydrated SQLite schema into LanceDB."""
    from src import lancedb_store

    metadata = schema.get("metadata", {})
    graph = schema.get("graph_nodes", {})
    details = schema.get("view_details", {})
    return lancedb_store.upsert_document(
        video_id=metadata.get("video_id", ""),
        text=(details.get("core_thesis") or "")
        + "\n"
        + (details.get("verbatim_quote") or ""),
        broadcast_date=metadata.get("broadcast_date"),
        source_channel=metadata.get("source_channel"),
        macro_theme=graph.get("macro_themes"),
        asset_class=graph.get("asset_classes"),
        ticker=graph.get("specific_tickers"),
        expectation_gap=schema.get("expectation_gap"),
        causal_chain=schema.get("causal_chain"),
        tracking_indicators=schema.get("tracking_indicators"),
        tactical_stance=schema.get("tactical_stance"),
        db_dir=lancedb_dir,
    )


def _schema_vector_document(schema: dict) -> dict:
    metadata = schema.get("metadata", {})
    graph = schema.get("graph_nodes", {})
    details = schema.get("view_details", {})
    return {
        "video_id": metadata.get("video_id", ""),
        "text": (details.get("core_thesis") or "")
        + "\n"
        + (details.get("verbatim_quote") or ""),
        "broadcast_date": metadata.get("broadcast_date"),
        "source_channel": metadata.get("source_channel"),
        "macro_theme": graph.get("macro_themes"),
        "asset_class": graph.get("asset_classes"),
        "ticker": graph.get("specific_tickers"),
        "expectation_gap": schema.get("expectation_gap"),
        "causal_chain": schema.get("causal_chain"),
        "tracking_indicators": schema.get("tracking_indicators"),
        "tactical_stance": schema.get("tactical_stance"),
    }


def upsert_schema_vectors(
    schemas: list[dict], lancedb_dir: Path | None = None
) -> bool:
    from src import lancedb_store

    return lancedb_store.upsert_documents(
        [_schema_vector_document(schema) for schema in schemas], db_dir=lancedb_dir
    )


def repair_missing(
    plan: ReconciliationPlan,
    schemas: dict[str, dict],
    *,
    markdown_exporter: ObsidianMDExporter,
    vector_upsert: Callable[[dict], bool] = upsert_schema_vector,
    vector_batch_upsert: Callable[[list[dict]], bool] | None = None,
) -> RepairResult:
    """Create missing projections only; orphan deletion is intentionally out of scope."""
    markdown_repaired: list[str] = []
    vectors_repaired: list[str] = []
    failures: list[str] = []
    for video_id in plan.missing_markdown:
        try:
            markdown_exporter.export_markdown(schemas[video_id])
            markdown_repaired.append(video_id)
        except Exception as exc:  # noqa: BLE001 - collect remaining repair outcomes
            failures.append(f"markdown:{video_id}:{type(exc).__name__}:{exc}")
    if plan.missing_vectors and vector_batch_upsert is not None:
        try:
            vector_schemas = [schemas[video_id] for video_id in plan.missing_vectors]
            if vector_batch_upsert(vector_schemas):
                vectors_repaired.extend(plan.missing_vectors)
            else:
                failures.append("vector:batch upsert returned false")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"vector:batch:{type(exc).__name__}:{exc}")
    else:
        for video_id in plan.missing_vectors:
            try:
                if vector_upsert(schemas[video_id]):
                    vectors_repaired.append(video_id)
                else:
                    failures.append(f"vector:{video_id}:upsert returned false")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"vector:{video_id}:{type(exc).__name__}:{exc}")
    return RepairResult(
        tuple(markdown_repaired), tuple(vectors_repaired), tuple(failures)
    )


def create_backup(
    db_path: Path,
    lancedb_dir: Path,
    backup_root: Path,
) -> Path:
    """Create a consistent SQLite snapshot and copy LanceDB before any repair writes."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = backup_root / stamp
    destination.mkdir(parents=True, exist_ok=False)
    if db_path.is_file():
        with sqlite3.connect(db_path) as source, sqlite3.connect(
            destination / db_path.name
        ) as target:
            source.backup(target)
    if lancedb_dir.is_dir():
        shutil.copytree(lancedb_dir, destination / lancedb_dir.name)
    manifest = {
        "created_at": stamp,
        "sqlite_source": str(db_path),
        "lancedb_source": str(lancedb_dir),
        "obsidian_note": "not copied; repair creates missing files and never overwrites/deletes",
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destination


def plan_as_dict(snapshot: StorageSnapshot, plan: ReconciliationPlan) -> dict:
    return {
        "counts": {
            "sqlite": len(snapshot.sqlite_ids),
            "markdown": len(snapshot.markdown_ids),
            "vectors": len(snapshot.vector_ids) if snapshot.vector_ids is not None else None,
        },
        "drift": asdict(plan),
        "repairable_count": plan.repairable_count,
        "has_drift": plan.has_drift,
    }
