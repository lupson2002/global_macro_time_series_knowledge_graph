# -*- coding: utf-8 -*-
"""
lancedb_store.py — LanceDB 임베디드 Vector DB 스토어 (TurboVec 대체, 신규 독립 모듈)
====================================================================================
기존 `.tvim` 기반 TurboVec 을 은퇴하고 초고성능 임베디드 Vector DB LanceDB 로 전면 교체.
SQLite(`data/macro_knowledge.db`)가 진실 원본 — LanceDB 는 시맨틱 검색용 파생 인덱스.

테이블: `data/lancedb_store` / `macro_vectors`
스키마: video_id, text, vector(FixedSizeList[256]), broadcast_date, source_channel,
        macro_theme(list[str]), asset_class(list[str]), ticker(list[str]),
        expectation_gap, causal_chain_json, tracking_indicators_json, tactical_stance_json

주요 기능:
  - upsert_document()      : 신규 수집 영상 단건 추가/갱신 (video_id 키 upsert)
  - search_hybrid()        : SQL 메타데이터 필터 + 시맨틱 벡터 하이브리드 검색
  - backfill_from_sqlite() : SQLite 전체 레코드 → LanceDB 일괄 적재
  - get_table_count()      : 적재 건수
  - hydrate_views()        : 검색 결과 video_id → SQLite 전체 리포트 필드 조인
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

import lancedb

from src.config import settings
from src.embedder import embed_one, embed_texts
from src.json_utils import parse_json_list

PROJECT_ROOT = settings.storage.project_root
DB_DIR = settings.storage.lancedb_dir
TABLE_NAME = "macro_vectors"
DB_PATH = settings.storage.sqlite_path

# ⚠️ embedder.DEFAULT_DIM 은 embedder 가 먼저 import 되면 .env 미로드로 256 이 동결될 수 있음.
# import 순서와 무관하게 .env(EMBEDDING_DIM=4096) 기준으로 직접 계산해 일관 유지.
VECTOR_DIM = settings.embedding.dimension


# ---------------------------------------------------------------------------
# 연결 / 테이블 관리
# ---------------------------------------------------------------------------
def _connect(db_dir: Path | None = None) -> lancedb.DBConnection:
    target = db_dir or DB_DIR
    target.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(target))


def _table_schema() -> pa.Schema:
    return pa.schema([
        pa.field("video_id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
        pa.field("broadcast_date", pa.string()),
        pa.field("source_channel", pa.string()),
        pa.field("macro_theme", pa.list_(pa.string())),
        pa.field("asset_class", pa.list_(pa.string())),
        pa.field("ticker", pa.list_(pa.string())),
        pa.field("expectation_gap", pa.string()),
        pa.field("causal_chain_json", pa.string()),
        pa.field("tracking_indicators_json", pa.string()),
        pa.field("tactical_stance_json", pa.string()),
    ])


def _get_table(create: bool = True, db_dir: Path | None = None):
    db = _connect(db_dir)
    if TABLE_NAME in db.table_names():
        return db.open_table(TABLE_NAME)
    if create:
        return db.create_table(TABLE_NAME, schema=_table_schema())
    return None


def _embed(text: str) -> list[float]:
    vec = embed_one(text, dim=VECTOR_DIM)
    return np.asarray(vec, dtype=np.float32).reshape(-1).tolist()


_EMBED_CHUNK = 10  # NIM nv-embed-v1 배치 지연(~0.5-6s) 대응 — 15s 타임아웃 내 완료되는 안전 청크


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """텍스트를 작은 청크로 나눠 remote 임베딩 — 일괄 대용량 요청의 타임아웃/해시 폴백 방지.
    (NIM /embeddings 이 배치당 수 초 걸려, 1,386건 단일 요청 시 15s 초과 → 해시 폴백됐음)
    """
    if not texts:
        return []
    out: list[np.ndarray] = []
    for i in range(0, len(texts), _EMBED_CHUNK):
        chunk = texts[i:i + _EMBED_CHUNK]
        vecs = embed_texts(chunk, dim=VECTOR_DIM)
        out.append(np.asarray(vecs, dtype=np.float32))
    return np.concatenate(out).reshape(len(texts), -1).tolist()


def _build_text(core_thesis: str = "", verbatim_quote: str = "", extra: str = "") -> str:
    """시맨틱 검색용 인덱스 텍스트 — thesis + 직접 인용 (+촉매) 결합."""
    parts = [p for p in (core_thesis or "", verbatim_quote or "", extra or "") if p and str(p).strip()]
    return "\n".join(parts).strip() or "(empty)"


def _safe_loads(raw):
    """JSON 문자열 안전 파싱 → list. NULL/빈/파손 → [] (backfill 방어)."""
    return parse_json_list(raw, accept_native=False)


# ---------------------------------------------------------------------------
# 단건 upsert (신규 수집 영상)
# ---------------------------------------------------------------------------
def upsert_document(
    video_id: str,
    text: str,
    broadcast_date: str = "",
    source_channel: str = "",
    macro_theme: list[str] | None = None,
    asset_class: list[str] | None = None,
    ticker: list[str] | None = None,
    expectation_gap: str = "",
    causal_chain: list | None = None,
    tracking_indicators: list | None = None,
    tactical_stance: list | None = None,
    db_dir: Path | None = None,
) -> bool:
    """추출 결과를 LanceDB 에 upsert (video_id 키). 실패해도 비파괴(경고만)."""
    if not video_id:
        return False
    try:
        row = {
            "video_id": video_id,
            "text": text,
            "vector": _embed(text),
            "broadcast_date": str(broadcast_date or ""),
            "source_channel": str(source_channel or ""),
            "macro_theme": list(macro_theme or []),
            "asset_class": list(asset_class or []),
            "ticker": list(ticker or []),
            "expectation_gap": str(expectation_gap or ""),
            "causal_chain_json": json.dumps(causal_chain or [], ensure_ascii=False),
            "tracking_indicators_json": json.dumps(tracking_indicators or [], ensure_ascii=False),
            "tactical_stance_json": json.dumps(tactical_stance or [], ensure_ascii=False),
        }
        table = _get_table(db_dir=db_dir)
        table.merge_insert("video_id").when_matched_update_all().when_not_matched_insert_all().execute([row])
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] LanceDB upsert 실패 {video_id}: {e}")
        return False


def upsert_documents(documents: list[dict], db_dir: Path | None = None) -> bool:
    """Batch upsert documents in one embedding pass and one Lance transaction."""
    valid = [document for document in documents if document.get("video_id")]
    if not valid:
        return True
    try:
        vectors = _embed_batch([str(document.get("text") or "") for document in valid])
        rows = []
        for document, vector in zip(valid, vectors):
            rows.append(
                {
                    "video_id": document["video_id"],
                    "text": str(document.get("text") or ""),
                    "vector": vector,
                    "broadcast_date": str(document.get("broadcast_date") or ""),
                    "source_channel": str(document.get("source_channel") or ""),
                    "macro_theme": list(document.get("macro_theme") or []),
                    "asset_class": list(document.get("asset_class") or []),
                    "ticker": list(document.get("ticker") or []),
                    "expectation_gap": str(document.get("expectation_gap") or ""),
                    "causal_chain_json": json.dumps(
                        document.get("causal_chain") or [], ensure_ascii=False
                    ),
                    "tracking_indicators_json": json.dumps(
                        document.get("tracking_indicators") or [], ensure_ascii=False
                    ),
                    "tactical_stance_json": json.dumps(
                        document.get("tactical_stance") or [], ensure_ascii=False
                    ),
                }
            )
        table = _get_table(db_dir=db_dir)
        table.merge_insert("video_id").when_matched_update_all().when_not_matched_insert_all().execute(rows)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] LanceDB batch upsert 실패 ({len(valid)}건): {exc}")
        return False


# ---------------------------------------------------------------------------
# 하이브리드 검색 (SQL 필터 + 시맨틱 벡터)
# ---------------------------------------------------------------------------
def search_hybrid(query: str, limit: int = 10, where_filter: str | None = None) -> list[dict[str, Any]]:
    """query 를 임베딩 → LanceDB ANN 검색 (+ 선택 SQL where_filter, 예: broadcast_date >= '2026-05-01').

    반환: 검색 순위대로 [ {video_id, text, broadcast_date, source_channel, ...} ] dict 목록.
    """
    table = _get_table(create=False)
    if table is None or table.count_rows() == 0:
        return []
    try:
        # embed_one 결과 차원을 강제 256 으로 정규화 (원격 임베딩 일시 실패/차원변동 방어)
        vec = np.asarray(embed_one(query, dim=VECTOR_DIM), dtype=np.float32).reshape(-1)
        qvec = np.resize(vec, VECTOR_DIM).reshape(1, -1)
        searcher = table.search(qvec).limit(max(1, int(limit)))
        if where_filter:
            searcher = searcher.where(where_filter)
        rows = searcher.to_list()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] LanceDB search 실패: {e}")
        return []


# ---------------------------------------------------------------------------
# SQLite 전체 백필
# ---------------------------------------------------------------------------
def backfill_from_sqlite() -> int:
    """SQLite `reports` 전체 → LanceDB 일괄 적재. 적재 건수 반환."""
    if not DB_PATH.exists():
        print(f"[ERROR] SQLite DB 없음: {DB_PATH}")
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT video_id, core_thesis, verbatim_quote, broadcast_date, source_channel, "
            "       expectation_gap, causal_chain, tracking_indicators, tactical_stance "
            "FROM reports WHERE core_thesis IS NOT NULL AND core_thesis != ''"
        ).fetchall()
        node_map: dict[str, dict[str, list[str]]] = {}
        for n in conn.execute("SELECT video_id, node_type, node_value FROM nodes"):
            vid = n["video_id"]
            node_map.setdefault(vid, {"macro_theme": [], "asset_class": [], "ticker": []})
            node_map[vid].setdefault(n["node_type"], []).append(n["node_value"])
    finally:
        conn.close()

    if not rows:
        print("[INFO] 백필할 reports 없음")
        return 0

    texts = [_build_text(r["core_thesis"], r["verbatim_quote"]) for r in rows]
    vecs = _embed_batch(texts)
    print(f"   🔁 LanceDB 백필: {len(rows)}건 임베딩 완료")

    records = []
    for r, vec in zip(rows, vecs):
        vid = r["video_id"]
        nodes = node_map.get(vid, {})
        records.append({
            "video_id": vid,
            "text": _build_text(r["core_thesis"], r["verbatim_quote"]),
            "vector": vec,
            "broadcast_date": str(r["broadcast_date"] or ""),
            "source_channel": str(r["source_channel"] or ""),
            "macro_theme": nodes.get("macro_theme", []),
            "asset_class": nodes.get("asset_class", []),
            "ticker": nodes.get("ticker", []),
            "expectation_gap": str(r["expectation_gap"] or ""),
            "causal_chain_json": json.dumps(_safe_loads(r["causal_chain"]), ensure_ascii=False),
            "tracking_indicators_json": json.dumps(_safe_loads(r["tracking_indicators"]), ensure_ascii=False),
            "tactical_stance_json": json.dumps(_safe_loads(r["tactical_stance"]), ensure_ascii=False),
        })

    # 전체 재적재 (overwrite 모드로 일괄 교체)
    db = _connect()
    if TABLE_NAME in db.table_names():
        db.drop_table(TABLE_NAME)
    db.create_table(TABLE_NAME, schema=_table_schema()).add(records)
    print(f"   ✅ LanceDB 백필 완료: {len(records)}건 → {DB_DIR / TABLE_NAME}")
    return len(records)


def get_table_count() -> int:
    table = _get_table(create=False)
    return table.count_rows() if table is not None else 0


def list_video_ids(db_dir: Path | None = None) -> frozenset[str]:
    """Return indexed video IDs without creating a missing LanceDB directory/table."""
    target = db_dir or DB_DIR
    if not target.exists():
        return frozenset()
    table = _get_table(create=False, db_dir=target)
    if table is None or table.count_rows() == 0:
        return frozenset()
    # Project only the identifier column. Direct to_arrow() materializes every vector;
    # to_lance() requires the optional pylance package, while search/select stays within
    # the installed LanceDB API and avoids loading high-dimensional vectors.
    id_table = table.search().select(["video_id"]).to_arrow()
    return frozenset(str(value) for value in id_table["video_id"].to_pylist())


# ---------------------------------------------------------------------------
# 검색 결과 → SQLite 전체 리포트 조인 (RAG 뷰 재구성)
# ---------------------------------------------------------------------------
def hydrate_views(video_ids: list[str]) -> list[dict[str, Any]]:
    """검색된 video_id 순서를 유지하며 SQLite(reports+quant_signals+nodes)로 전체 뷰 조인."""
    if not video_ids:
        return []
    placeholders = ",".join("?" * len(video_ids))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        reports = {r["video_id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM reports WHERE video_id IN ({placeholders})", video_ids)}
        sigs = {s["video_id"]: dict(s) for s in conn.execute(
            f"SELECT * FROM quant_signals WHERE video_id IN ({placeholders})", video_ids)}
        nodes = conn.execute(
            f"SELECT video_id, node_type, node_value FROM nodes WHERE video_id IN ({placeholders})", video_ids).fetchall()
    finally:
        conn.close()

    nodes_by_vid: dict[str, dict[str, list[str]]] = {}
    for n in nodes:
        nodes_by_vid.setdefault(n["video_id"], {"macro_theme": [], "asset_class": [], "ticker": []})
        nodes_by_vid[n["video_id"]].setdefault(n["node_type"], []).append(n["node_value"])

    out = []
    for vid in video_ids:
        r = reports.get(vid)
        if not r:
            continue
        s = sigs.get(vid, {})
        out.append({
            "video_id": vid,
            "speaker_name": r.get("speaker_name"),
            "speaker_role": r.get("speaker_role"),
            "source_channel": r.get("source_channel"),
            "broadcast_date": r.get("broadcast_date"),
            "time_box": r.get("time_box"),
            "core_thesis": r.get("core_thesis"),
            "verbatim_quote": r.get("verbatim_quote"),
            "conditional_catalysts": parse_json_list(r.get("conditional_catalysts"), accept_native=False),
            "invalidation_risks": parse_json_list(r.get("invalidation_risks"), accept_native=False),
            "key_data_points": parse_json_list(r.get("key_data_points"), accept_native=False),
            "additional_quotes": parse_json_list(r.get("additional_quotes"), accept_native=False),
            "price_targets": parse_json_list(r.get("price_targets"), accept_native=False),
            "speaker_institution": r.get("speaker_institution"),
            "expectation_gap": r.get("expectation_gap"),
            "causal_chain": parse_json_list(r.get("causal_chain"), accept_native=False),
            "tracking_indicators": parse_json_list(r.get("tracking_indicators"), accept_native=False),
            "tactical_stance": parse_json_list(r.get("tactical_stance"), accept_native=False),
            "bull_bear_score": s.get("bull_bear_score"),
            "conviction_score": s.get("conviction_score"),
            "contrarian_flag": bool(s.get("contrarian_flag")),
            "sector_tilt": s.get("sector_tilt"),
            "duration_call": s.get("duration_call"),
            "macro_factor": s.get("macro_factor"),
            "view_time_horizon": s.get("view_time_horizon"),
            "macro_themes": nodes_by_vid.get(vid, {}).get("macro_theme", []),
            "asset_classes": nodes_by_vid.get(vid, {}).get("asset_class", []),
            "tickers": nodes_by_vid.get(vid, {}).get("ticker", []),
        })
    return out


# ---------------------------------------------------------------------------
# MCP/텔레그램 tool 호환 래퍼 (구 turbovec_server 대체)
# ---------------------------------------------------------------------------
def semantic_search_macro(query_text: str, top_k: int = 5) -> str:
    """LanceDB 하이브리드 검색 → JSON 문자열 (구 turbovec_server.semantic_search_macro 호환)."""
    rows = search_hybrid(query_text, limit=top_k)
    views = hydrate_views([r["video_id"] for r in rows])
    return json.dumps({
        "query": query_text,
        "backend": "lancedb",
        "top_k": top_k,
        "result_count": len(views),
        "results": views,
    }, ensure_ascii=False, indent=2)


def get_vect_index_status() -> str:
    """LanceDB 인덱스 상태 진단 JSON (구 turbovec_server.get_vect_index_status 호환)."""
    from src.embedder import backend_name
    return json.dumps({
        "embedding_backend": backend_name(),
        "vector_db": "lancedb",
        "table": TABLE_NAME,
        "table_path": str(DB_DIR / TABLE_NAME),
        "total_vectors": get_table_count(),
        "in_memory_index_ready": True,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    n = backfill_from_sqlite()
    print(f"LanceDB Total Rows: {get_table_count()} (backfilled {n})")
