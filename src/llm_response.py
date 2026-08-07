"""Provider-neutral processing stages for structured LLM responses."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any


def _ensure_double_brackets(value: str) -> str:
    value = value.strip()
    if not value.startswith("[["):
        value = "[[" + value
    if not value.endswith("]]"):
        value += "]]"
    return value


def post_process_json(data: dict) -> dict:
    """Normalize graph values to the established Obsidian backlink format."""
    try:
        nodes = data.get("graph_nodes", {})
        if "time_box" in nodes and nodes["time_box"]:
            nodes["time_box"] = _ensure_double_brackets(nodes["time_box"])
        for key in ("macro_themes", "asset_classes", "specific_tickers"):
            if key in nodes and isinstance(nodes[key], list):
                nodes[key] = [
                    _ensure_double_brackets(item) for item in nodes[key] if item
                ]

        quant = data.get("quant_signals", {})
        if isinstance(quant, dict) and quant.get("sector_tilt"):
            quant["sector_tilt"] = _ensure_double_brackets(quant["sector_tilt"])
        if isinstance(quant, dict):
            for key in ("duration_call", "macro_factor", "view_time_horizon"):
                value = quant.get(key)
                if (
                    isinstance(value, str)
                    and value.startswith("[[")
                    and value.endswith("]]")
                ):
                    quant[key] = value[2:-2]

        view = data.get("view_details", {})
        if isinstance(view, dict) and isinstance(view.get("price_targets"), list):
            for target in view["price_targets"]:
                if isinstance(target, dict) and target.get("ticker"):
                    target["ticker"] = _ensure_double_brackets(str(target["ticker"]))
    except Exception as exc:  # noqa: BLE001 - compatibility normalization is non-fatal
        print(f"[WARN] Post-processing JSON brackets failed: {exc}")
    return data


def extract_json(text: str) -> str:
    """Extract the first balanced JSON object from provider text."""
    if not text:
        raise ValueError("Empty LLM response")
    value = text.strip().lstrip("\ufeff\u200b\u200c\u200d")
    fence_match = re.search(
        r"```(?:json)?\s*\n(.*?)\n```", value, re.DOTALL | re.IGNORECASE
    )
    if fence_match:
        value = fence_match.group(1).strip()
    else:
        if value.startswith("```"):
            value = value[3:]
            if value.lower().startswith("json"):
                value = value[4:]
            value = value.lstrip("\n")
        if value.endswith("```"):
            value = value[:-3]
        value = value.strip()

    first = value.find("{")
    if first == -1:
        raise ValueError(
            f"No JSON object start '{{' found in LLM response (first 200 chars): {value[:200]!r}"
        )

    depth = 0
    in_string = False
    escape_next = False
    for index in range(first, len(value)):
        character = value[index]
        if escape_next:
            escape_next = False
            continue
        if character == "\\":
            escape_next = True
            continue
        if character == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return value[first : index + 1]

    last = value.rfind("}")
    if last == -1 or last <= first:
        raise ValueError(
            f"Unbalanced JSON braces in LLM response (first 200 chars): {value[:200]!r}"
        )
    return value[first : last + 1]


def apply_trusted_metadata(
    data: Any,
    *,
    video_id: str,
    source_channel: str,
    upload_date: str | None,
) -> Any:
    """Override model-inferred source fields with trusted ingestion metadata."""
    if not isinstance(data, dict) or "metadata" not in data:
        return data
    metadata = data["metadata"]
    if not isinstance(metadata, dict):
        return data
    metadata["video_id"] = video_id
    if source_channel:
        metadata["source_channel"] = source_channel
    if upload_date:
        metadata["broadcast_date"] = upload_date
    return data


class ExtractionResponseProcessor:
    """Parse, recover once, normalize, override, and soft-validate a response."""

    def __init__(self, validator: Callable[[Any], Any]):
        self.validator = validator

    def process(
        self,
        raw_text: str,
        *,
        video_id: str,
        source_channel: str,
        upload_date: str | None,
        recover: Callable[[], str] | None = None,
    ) -> dict:
        attempts = 2 if recover is not None else 1
        for attempt in range(attempts):
            try:
                parsed = json.loads(extract_json(raw_text))
                parsed = apply_trusted_metadata(
                    parsed,
                    video_id=video_id,
                    source_channel=source_channel,
                    upload_date=upload_date,
                )
                parsed = post_process_json(parsed)
                try:
                    self.validator(parsed)
                except Exception as exc:  # noqa: BLE001 - validation remains advisory
                    print(
                        "   [WARN] Pydantic schema validation soft-fail "
                        f"(data kept as-is): {exc}"
                    )
                return parsed
            except (json.JSONDecodeError, ValueError) as exc:
                print(
                    f"   [WARN] JSON parse attempt {attempt + 1} failed: {exc}\n"
                    f"   [DEBUG] Raw response (first 400 chars): {raw_text[:400]!r}"
                )
                if attempt + 1 < attempts:
                    print(
                        "   [RETRY] Re-issuing JSON-mode call to recover parseable output..."
                    )
                    raw_text = recover()
                    continue
                raise RuntimeError(
                    f"JSON parsing failed after {attempts} attempts. "
                    f"Last error: {exc}. Raw response (first 500 chars): {raw_text[:500]!r}"
                ) from exc

        raise AssertionError("response processing loop exited unexpectedly")
