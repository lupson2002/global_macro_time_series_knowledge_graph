"""Pure rendering helpers shared by generated reports."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import markdown


def render_frontmatter(fields: Iterable[tuple[str, Any]]) -> str:
    """Render ordered, already-normalized YAML fields with the legacy envelope."""
    lines = ["---", *(f"{key}: {value}" for key, value in fields), "---", ""]
    return "\n".join(lines) + "\n"


def markdown_table_cell(value: Any, *, flatten: bool = True, strip: bool = True) -> str:
    """Escape a Markdown table cell while allowing legacy whitespace policies."""
    rendered = str(value or "").replace("|", "\\|")
    if flatten:
        rendered = rendered.replace("\n", " ")
    return rendered.strip() if strip else rendered


def markdown_to_email_html(md_content: str) -> str:
    """Render Markdown with the established Gmail-compatible inline-style envelope."""
    cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", md_content)
    html_body = markdown.markdown(
        cleaned,
        extensions=["tables", "sane_lists", "nl2br"],
        output_format="html",
    )
    style = """
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
                font-size:14px;line-height:1.6;color:#1f2329;max-width:720px;margin:0 auto;">
    """
    styled = re.sub(r"<h1", "<h1 style='font-size:20px;border-bottom:2px solid #d0d7de;padding-bottom:6px;margin:18px 0 12px;'", html_body)
    styled = re.sub(r"<h2", "<h2 style='font-size:16px;color:#0969da;border-left:4px solid #0969da;padding-left:8px;margin:18px 0 10px;'", styled)
    styled = re.sub(r"<h3", "<h3 style='font-size:14px;color:#1f2328;margin:14px 0 8px;'", styled)
    styled = re.sub(r"<table", "<table style='border-collapse:collapse;width:100%;font-size:13px;margin:12px 0;'", styled)
    styled = re.sub(r"<th", "<th style='border:1px solid #d0d7de;background:#f6f8fa;padding:6px 8px;text-align:left;'", styled)
    styled = re.sub(r"<td", "<td style='border:1px solid #d0d7de;padding:6px 8px;vertical-align:top;'", styled)
    styled = re.sub(
        r"<blockquote",
        "<blockquote style='border-left:4px solid #0969da;background:#f6f8fa;margin:10px 0;padding:8px 12px;color:#57606a;'",
        styled,
    )
    styled = re.sub(r"<strong", "<strong style='color:#cf222e;'", styled)
    styled = re.sub(r"<hr", "<hr style='border:none;border-top:1px solid #d0d7de;margin:16px 0;'", styled)
    return style + styled + "</div>"
