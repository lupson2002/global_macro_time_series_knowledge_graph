import unittest

from src.domain import MacroView
from tests.helpers import macro_view


class MacroViewDomainTests(unittest.TestCase):
    def test_sections_are_read_only_and_non_mapping_sections_are_safe(self):
        raw = {
            "metadata": {"video_id": "abcdefghijk"},
            "graph_nodes": None,
            "quant_signals": "invalid",
            "view_details": [],
            "causal_chain": "invalid",
        }

        view = MacroView.from_mapping(raw)

        self.assertEqual(view.video_id, "abcdefghijk")
        self.assertEqual(dict(view.graph_nodes), {})
        self.assertEqual(dict(view.quant_signals), {})
        self.assertEqual(dict(view.view_details), {})
        self.assertEqual(view.list_value("causal_chain"), [])
        with self.assertRaises(TypeError):
            view.metadata["video_id"] = "replacement"

    def test_vector_document_is_the_canonical_storage_projection(self):
        raw = macro_view()

        document = MacroView.from_mapping(raw).vector_document()

        self.assertEqual(
            document,
            {
                "video_id": "abcdefghijk",
                "text": 'Rates fall after the "peak".\nThe "peak" is behind us.',
                "broadcast_date": "2026-08-07",
                "source_channel": "Test_Channel",
                "macro_theme": ["[[Inflation]]"],
                "asset_class": ["[[Bonds]]"],
                "ticker": ["[[TLT]]"],
                "expectation_gap": "Market prices sticky inflation.",
                "causal_chain": ["CPI down", "Yields down", "Bonds up"],
                "tracking_indicators": raw["tracking_indicators"],
                "tactical_stance": raw["tactical_stance"],
            },
        )

    def test_list_value_returns_a_copy_to_protect_source_data(self):
        raw = macro_view()
        view = MacroView.from_mapping(raw)

        causal_chain = view.list_value("causal_chain")
        causal_chain.append("new")

        self.assertNotIn("new", raw["causal_chain"])
