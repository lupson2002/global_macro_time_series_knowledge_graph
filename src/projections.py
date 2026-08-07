"""Derived storage adapters for macro views."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.domain import MacroView


class LanceDbProjection:
    """Project a macro view into the rebuildable LanceDB search store."""

    def __init__(self, upsert: Callable[..., bool] | None = None):
        self._upsert = upsert

    def project(self, data: Mapping[str, Any]) -> None:
        upsert = self._upsert
        if upsert is None:
            from src.lancedb_store import upsert_document

            upsert = upsert_document
        if not upsert(**MacroView.from_mapping(data).vector_document()):
            raise RuntimeError("LanceDB upsert returned false")
