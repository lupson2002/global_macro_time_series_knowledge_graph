"""Small JSON normalization helpers shared by derived pipelines."""

from __future__ import annotations

import json
from typing import Any


def parse_json_list(raw: Any, *, accept_native: bool = True) -> list:
    """Return a JSON array as a list; empty, malformed and non-list values become ``[]``.

    ``accept_native=False`` preserves the stricter SQLite-column contract used by
    legacy Daily/exporter call sites, where only serialized JSON is accepted.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return raw if accept_native else []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []
