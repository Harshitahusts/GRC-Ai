"""Markdown to HTML for the GRC Analyst's replies.

Raw HTML in the Markdown is escaped, not rendered, so a reply can't inject scripts.
"""

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def render_markdown(text: str) -> str:
    return _md.render(text)
