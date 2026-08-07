import json
import unittest
from pathlib import Path

import pandas as pd

from scripts.insight_report import _matrix_headline
from scripts.insights.market_narrative import _view_line_with_nodes
from src.orchestrator import (
    _append_visual_links,
    _extract_viz_json,
    _render_report_block,
    _replace_json_block_with_tables,
    _viz_json_to_markdown,
)
from src.report_generator import calculate_deterministic_sentiment
from src.report_generator import _assemble_daily_outputs
from src.telegram_bot import TOOL_REGISTRY_INPROC, _chunk_lines, dispatch_tool


class DailyReportContractTests(unittest.TestCase):
    def test_file_and_email_bodies_only_differ_by_frontmatter(self):
        file_body, email_body = _assemble_daily_outputs("yaml\n", "body", "evidence", "summary")
        self.assertEqual(file_body, "yaml\nbodyevidencesummary")
        self.assertEqual(email_body, "bodyevidencesummary")

    def test_sentiment_weighting_tail_deduction_and_regime(self):
        result = calculate_deterministic_sentiment([
            {"bull_bear_score": 8, "conviction_score": 2},
            {"bull_bear_score": 2, "conviction_score": 8},
        ])
        self.assertEqual(result, {
            "sample_count": 2, "raw_weighted_avg": 3.2, "stddev": 4.24,
            "tail_risk_count": 1, "deduction": 1.5, "adjusted_score": 1.7,
            "sentiment_regime": "Extreme Panic / Cash Focus",
        })

    def test_sentiment_ignores_invalid_rows_and_keeps_empty_neutral(self):
        result = calculate_deterministic_sentiment([
            {"bull_bear_score": "bad", "conviction_score": 8},
            {"bull_bear_score": 7},
            {"bull_bear_score": 9, "conviction_score": 0},
        ])
        self.assertEqual(result["sample_count"], 0)
        self.assertIsNone(result["adjusted_score"])
        self.assertEqual(result["sentiment_regime"], "Neutral / Wait-and-See")


class CioReportContractTests(unittest.TestCase):
    def test_visual_links_keep_declared_order_and_original_body(self):
        rendered = _append_visual_links("report", {
            "conflicts": Path("conflicts.html"), "pie": Path("pie.html"), "bar": None,
        })
        self.assertTrue(rendered.startswith("report\n\n---"))
        self.assertLess(rendered.index("pie.html"), rendered.index("conflicts.html"))
        self.assertEqual(_append_visual_links("report", {}), "report")

    def test_visual_json_is_extracted_rendered_and_replaces_fence(self):
        payload = {
            "allocation": [{"asset": "Rates|Cash", "weight": 25, "rationale": "defensive"}],
            "conflicts": [{"topic": "Inflation", "long_guru": "A", "long_view": "sticky",
                           "short_guru": "B", "short_view": "falling"}],
        }
        report = "# CIO\n\n```json\n" + json.dumps(payload) + "\n```\n\nEnd"
        extracted = _extract_viz_json(report)
        table = _viz_json_to_markdown(extracted)
        rendered = _replace_json_block_with_tables(report, extracted)
        self.assertEqual(extracted, payload)
        self.assertIn("Rates\\|Cash", table)
        self.assertIn("### 📊 자산 배분 요약", rendered)
        self.assertIn("### ⚔️ 핵심 갈등 요약", rendered)
        self.assertNotIn("```json", rendered)
        self.assertTrue(rendered.endswith("End"))

    def test_context_block_preserves_evidence_and_narrative_fields(self):
        rendered = _render_report_block(1, {
            "speaker_name": "Analyst", "speaker_role": "CIO", "source_channel": "Desk",
            "broadcast_date": "2026-08-07", "time_box": "3M", "bull_bear_score": 7,
            "conviction_score": 9, "core_thesis": "Buy duration",
            "verbatim_quote": "Rates peak here", "conditional_catalysts": '["CPI cools"]',
            "invalidation_risks": '["CPI rises"]', "causal_chain": '["CPI", "Rates"]',
        }, contrarian=True)
        for fragment in ('Verbatim Quote: "Rates peak here"', "Catalysts: CPI cools",
                         "Invalidation Risks: CPI rises", "Causal Chain: CPI -> Rates", "Contrarian"):
            self.assertIn(fragment, rendered)


class TelegramContractTests(unittest.IsolatedAsyncioTestCase):
    def test_chunking_prefers_line_boundaries_and_forces_long_lines(self):
        text = "alpha\nbeta\ngamma"
        chunks = _chunk_lines(text, 10)
        self.assertEqual("\n".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))
        forced = _chunk_lines("abcdefgh", 3)
        self.assertEqual("".join(forced), "abcdefgh")
        self.assertTrue(all(len(chunk) <= 3 for chunk in forced))

    async def test_dispatch_parses_json_and_exposes_failures(self):
        async def echo(value):
            return {"value": value}

        original = TOOL_REGISTRY_INPROC.get("contract_echo")
        TOOL_REGISTRY_INPROC["contract_echo"] = echo
        try:
            self.assertEqual(json.loads(await dispatch_tool("contract_echo", '{"value": 7}')),
                             {"value": 7})
            mismatch = json.loads(await dispatch_tool("contract_echo", {"wrong": 1}))
            self.assertIn("argument mismatch", mismatch["error"])
            unknown = json.loads(await dispatch_tool("missing_contract_tool", {}))
            self.assertEqual(unknown, {"error": "Unknown tool: missing_contract_tool"})
        finally:
            if original is None:
                TOOL_REGISTRY_INPROC.pop("contract_echo", None)
            else:
                TOOL_REGISTRY_INPROC["contract_echo"] = original


class InsightReportContractTests(unittest.TestCase):
    def test_asset_headline_and_empty_fallback(self):
        frame = pd.DataFrame([
            {"asset_class": "Equity", "avg_bull_bear": 8.0, "contrarian_pct": 10},
            {"asset_class": "Rates", "avg_bull_bear": 3.0, "contrarian_pct": 60},
        ])
        headline = _matrix_headline(frame, "asset")
        self.assertIn("**Equity** 8.0 최고 bull", headline)
        self.assertIn("**Rates** 3.0 최고 bear", headline)
        self.assertIn("**Rates** contrarian 60%", headline)
        self.assertEqual(_matrix_headline(pd.DataFrame(), "asset"), "데이터 부족으로 핵치 도출 불가")

    def test_narrative_view_keeps_graph_nodes(self):
        line = _view_line_with_nodes({
            "speaker_name": "Guru", "core_thesis": "Dollar weakens",
            "macro_themes": ["Inflation"], "asset_classes": ["FX"], "tickers": ["DXY"],
        })
        self.assertIn("nodes=Inflation, FX, DXY", line)


if __name__ == "__main__":
    unittest.main()
