"""Read-only domain boundary for extracted macro views."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypedDict


class VectorDocument(TypedDict):
    video_id: str
    text: str
    broadcast_date: Any
    source_channel: Any
    macro_theme: list
    asset_class: list
    ticker: list
    expectation_gap: Any
    causal_chain: list
    tracking_indicators: list
    tactical_stance: list


def _section(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class MacroView:
    """Compatibility adapter over the public dict schema.

    The adapter does not validate or coerce valid extraction values. It makes section
    access read-only, treats malformed sections as empty, and owns shared projections.
    """

    raw: Mapping[str, Any]
    metadata: Mapping[str, Any]
    graph_nodes: Mapping[str, Any]
    quant_signals: Mapping[str, Any]
    view_details: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "MacroView":
        if not isinstance(data, Mapping):
            raise TypeError("MacroView requires a mapping")
        root = MappingProxyType(dict(data))
        return cls(
            raw=root,
            metadata=_section(root.get("metadata")),
            graph_nodes=_section(root.get("graph_nodes")),
            quant_signals=_section(root.get("quant_signals")),
            view_details=_section(root.get("view_details")),
        )

    @property
    def video_id(self) -> str:
        value = self.metadata.get("video_id")
        return value if isinstance(value, str) else ""

    def list_value(self, name: str) -> list:
        value = self.raw.get(name)
        return list(value) if isinstance(value, list) else []

    def section_list(self, section: Mapping[str, Any], name: str) -> list:
        value = section.get(name)
        return list(value) if isinstance(value, list) else []

    def vector_document(self) -> VectorDocument:
        thesis = self.view_details.get("core_thesis")
        quote = self.view_details.get("verbatim_quote")
        return {
            "video_id": self.video_id,
            "text": (thesis if isinstance(thesis, str) else "")
            + "\n"
            + (quote if isinstance(quote, str) else ""),
            "broadcast_date": self.metadata.get("broadcast_date"),
            "source_channel": self.metadata.get("source_channel"),
            "macro_theme": self.section_list(self.graph_nodes, "macro_themes"),
            "asset_class": self.section_list(self.graph_nodes, "asset_classes"),
            "ticker": self.section_list(self.graph_nodes, "specific_tickers"),
            "expectation_gap": self.raw.get("expectation_gap"),
            "causal_chain": self.list_value("causal_chain"),
            "tracking_indicators": self.list_value("tracking_indicators"),
            "tactical_stance": self.list_value("tactical_stance"),
        }
