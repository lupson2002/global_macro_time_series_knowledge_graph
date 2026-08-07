import unittest

from src.report_generator import _build_frontmatter, _md_to_html_email
from src.report_rendering import markdown_table_cell, render_frontmatter


class FrontmatterRenderingTests(unittest.TestCase):
    def test_order_and_legacy_envelope_are_exact(self):
        rendered = render_frontmatter((("date", "2026-08-07"), ("type", "daily")))
        self.assertEqual(rendered, "---\ndate: 2026-08-07\ntype: daily\n---\n\n")

    def test_daily_frontmatter_snapshot_is_unchanged(self):
        self.assertEqual(
            _build_frontmatter("2026-08-07", "model-x", 3, "2026-08-07T06:00:00+09:00"),
            "---\n"
            "date: 2026-08-07\n"
            "type: daily_macro_synthesis\n"
            "model: model-x\n"
            "provider: nim\n"
            "source_videos: 3\n"
            "generated_at: 2026-08-07T06:00:00+09:00\n"
            "tags: [macro, daily_synthesis]\n"
            "---\n\n",
        )


class MarkdownRenderingTests(unittest.TestCase):
    def test_table_cell_escape_preserves_configured_whitespace_policy(self):
        self.assertEqual(markdown_table_cell(" A|B\nC "), "A\\|B C")
        self.assertEqual(
            markdown_table_cell(" A|B\nC ", flatten=False, strip=False),
            " A\\|B\nC ",
        )

    def test_email_html_snapshot_keeps_backlink_stripping_and_inline_styles(self):
        rendered = _md_to_html_email("# [[Rates]]\n\n| A |\n|---|\n| **B** |")
        self.assertNotIn("[[", rendered)
        self.assertIn(">Rates</h1>", rendered)
        self.assertIn("font-size:20px", rendered)
        self.assertIn("border-collapse:collapse", rendered)
        self.assertIn("color:#cf222e", rendered)
        self.assertTrue(rendered.endswith("</div>"))


if __name__ == "__main__":
    unittest.main()
