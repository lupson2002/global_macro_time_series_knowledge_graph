# -*- coding: utf-8 -*-
"""
Embedding Provider for LanceDB Indexing
==================================================
Pluggable embedding backend for `core_thesis` → fixed-dim float32 vectors.

Resolution order (first match wins):
  1. `EMBEDDING_API_URL` + `EMBEDDING_API_KEY` — remote endpoint (Ollama Pro /1/api/embed,
     OpenAI /v1/embeddings, custom HTTP)
  2. `EMBEDDING_LOCAL_MODEL` — local sentence-transformers via HF model id
  3. Deterministic hashed-bag-of-words fallback (always available, no network)
     — 256-dim L2-normalized. Lower semantic quality but enables index bring-up
       on the mini PC without GPU/internet. The fallback is *deterministic*,
     so a corpus indexed via fallback can be re-ranked after upgrading the
     backend without changing stored vectors (only re-embed).

Output: numpy.ndarray shape (dim,), dtype float32, L2-normalized.
"""
from __future__ import annotations

import hashlib
import math
import logging
from typing import List, Optional

import numpy as np
import requests

from src.config import settings

logger = logging.getLogger(__name__)

# Default output dim. 256 keeps `.tvec` files small and is friendly to the
# mini-PC's RAM budget while preserving enough resolution for ANN search.
# 👑 [B2] env override (EMBEDDING_DIM) — remote 모델 dim 맞출 때 (예: nv-embed-v1=4096).
DEFAULT_DIM = settings.embedding.dimension


# ---------------------------------------------------------------------------
# Backend A: Remote HTTP embedding API (Ollama Pro, OpenAI, custom)
# ---------------------------------------------------------------------------
def _embed_remote(texts: List[str], dim: int) -> Optional[np.ndarray]:
    """Try a remote embedding endpoint. Returns None on any failure.

    Supports two API shapes by autodetect of response field:
      • Ollama Pro: { "embeddings": [[...]] }
      • OpenAI:     { "data": [{"embedding": [...]}, ...] }
    """
    url = settings.embedding.api_url
    if not url:
        return None
    api_key = settings.embedding.api_key
    model = settings.embedding.model

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        # Ollama Pro (/api/embed) 와 OpenAI (/v1/embeddings) 모두 동일 payload 스펙.
        # (이전 if/else 분기는 양 갈래가 동일 payload 를 만드는 dead conditional 이었음.)
        payload = {"model": model, "input": texts}

        r = requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()

        # Ollama Pro shape
        if "embeddings" in data and isinstance(data["embeddings"], list):
            vecs = np.asarray(data["embeddings"], dtype=np.float32)
        # OpenAI shape
        elif "data" in data and isinstance(data["data"], list):
            vecs = np.asarray([d["embedding"] for d in data["data"]], dtype=np.float32)
        else:
            logger.warning("[embedder] unknown response shape: keys=%s", list(data.keys()))
            return None

        if vecs.ndim != 2 or vecs.shape[0] != len(texts):
            logger.warning("[embedder] unexpected vector shape: %s", vecs.shape)
            return None
        if vecs.shape[1] != dim:
            logger.info("[embedder] remote dim %d != target %d; will resize", vecs.shape[1], dim)
        return vecs
    except Exception as e:
        logger.warning("[embedder] remote embed failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Backend B: Local sentence-transformers (heavy, requires torch)
# ---------------------------------------------------------------------------
# 👑 [A18] 모델 싱글톤 캐시 — 매 호출마다 모델 로드(수 초) 방지.
# 동일 model_id 에 대해 동일 인스턴스 재사용(동일 입력 → 동일 벡터, behavior preserved).
_ST_MODELS: dict = {}


def _get_st_model(model_id: str):
    if model_id not in _ST_MODELS:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _ST_MODELS[model_id] = SentenceTransformer(model_id)
    return _ST_MODELS[model_id]


def _embed_local_st(texts: List[str], dim: int) -> Optional[np.ndarray]:
    model_id = settings.embedding.local_model
    if not model_id:
        return None
    try:
        model = _get_st_model(model_id)
        vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        vecs = np.asarray(vecs, dtype=np.float32)
        if vecs.ndim == 1:
            vecs = vecs.reshape(1, -1)
        return vecs
    except Exception as e:
        logger.warning("[embedder] local ST failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Backend C: Deterministic hash-BoW fallback (always available)
# ---------------------------------------------------------------------------
def _embed_hash_fallback(texts: List[str], dim: int) -> np.ndarray:
    """256/512-dim hashed bag-of-words with sublinear TF and L2 norm.
    Deterministic: same text → same vector. Not semantic, but stable for ANN
    and good enough to bring the system online before plugging in a real model.
    """
    vecs = np.zeros((len(texts), dim), dtype=np.float32)
    for i, t in enumerate(texts):
        # Tokenize on whitespace + lowercase
        tokens = (t or "").lower().split()
        if not tokens:
            vecs[i, 0] = 1.0  # never-empty vector
            continue
        for tok in tokens:
            # Stable 64-bit hash → mod dim
            h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big")
            idx = h % dim
            # 👑 [D5] 가중 BOW — 1.0/log1p(2.0) 상수 배수(선형 스케일).
            # 모듈 docstring "sublinear TF" 는 부정확 — log(1+tf) 가 아닌 상수 가중.
            vecs[i, idx] += 1.0 / math.log1p(2.0)
    # L2 normalize
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
# 👑 [A19] 실제 성공 백엔드 추적 — env 기준이 아닌 실제 사용 백엔드 보고.
_LAST_SUCCESS_BACKEND: Optional[str] = None


def embed_texts(texts: List[str], dim: int = DEFAULT_DIM) -> np.ndarray:
    """Embed a batch of texts. Returns (N, dim) float32, L2-normalized.

    Backend resolution: remote → local ST → hash fallback.
    👑 [B2] 차원 불일치 시 truncate 대신 거부 + 폴백 — 1536-dim 을 256-dim
    truncate 시 첫 256 성분만 남아 ANN 품질이 의미 없게 파괴됨. 동일 차원
    모델만 허용(거부 시 해시 폴백).
    """
    global _LAST_SUCCESS_BACKEND
    if not texts:
        return np.zeros((0, dim), dtype=np.float32)

    # Backend A
    vecs = _embed_remote(texts, dim)
    if vecs is not None:
        if vecs.shape[1] == dim:
            _LAST_SUCCESS_BACKEND = f"remote:{settings.embedding.api_url}"
            return vecs.astype(np.float32, copy=False)
        logger.warning(
            "[embedder] remote dim %d != target %d — rejecting truncate "
            "(quality destructive). Falling back. Configure a %d-dim model.",
            vecs.shape[1], dim, dim,
        )

    # Backend B
    vecs = _embed_local_st(texts, dim)
    if vecs is not None:
        if vecs.shape[1] == dim:
            _LAST_SUCCESS_BACKEND = f"local-st:{settings.embedding.local_model}"
            return vecs.astype(np.float32, copy=False)
        logger.warning(
            "[embedder] local ST dim %d != target %d — rejecting truncate, falling back.",
            vecs.shape[1], dim,
        )

    # Backend C (always available)
    _LAST_SUCCESS_BACKEND = "hash-fallback"
    return _embed_hash_fallback(texts, dim)


def embed_one(text: str, dim: int = DEFAULT_DIM) -> np.ndarray:
    return embed_texts([text], dim)[0]


def _resize_batch(vecs: np.ndarray, target_dim: int) -> np.ndarray:
    """Truncate or zero-pad to target_dim; re-normalize.

    ⚠️ [B2] 현재 embed_texts 경로에서는 호출 안 함(dim 불일치 시 거부+폴백).
    향후 명시적 차원 축소 의도 시 사용하도록 남겨둠.
    """
    n, d = vecs.shape
    if d == target_dim:
        return vecs.astype(np.float32, copy=False)
    if d > target_dim:
        out = vecs[:, :target_dim]
    else:
        out = np.zeros((n, target_dim), dtype=np.float32)
        out[:, :d] = vecs
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


def backend_name() -> str:
    """Diagnostic: 실제 성공한 백엔드 우선 보고. 없으면 env 기준 기대값.

    이전엔 env 기준만 보고 → remote 구성했지만 인증 실패로 폴백한 경우에도
    'remote' 로 거짓 보고. 이제 _LAST_SUCCESS_BACKEND 로 실제 사용 백엔드.
    """
    if _LAST_SUCCESS_BACKEND is not None:
        return _LAST_SUCCESS_BACKEND
    if settings.embedding.api_url:
        return f"remote:{settings.embedding.api_url}"
    if settings.embedding.local_model:
        return f"local-st:{settings.embedding.local_model}"
    return "hash-fallback"


if __name__ == "__main__":
    v = embed_texts(["Inflation is structural in 2026", "AI infrastructure capex will boom"], dim=256)
    print(f"backend={backend_name()}, shape={v.shape}, dtype={v.dtype}")
    print(f"row0 norm={float(np.linalg.norm(v[0])):.3f}, row1 norm={float(np.linalg.norm(v[1])):.3f}")
