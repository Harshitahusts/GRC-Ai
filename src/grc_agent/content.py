"""Docs and blog posts: Markdown content, SEO checks, and first-run seeding.

Content lives in the database so it can be written and edited in the app. The
starter set ships as Markdown files in grc_agent/content_seed/ and is imported
once, as drafts, the first time the app starts. Imports never overwrite edits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

from markdown_it import MarkdownIt

TYPES = {"docs": "Docs", "blog": "Blog"}

# Raw HTML in Markdown is escaped, not rendered, so content can't inject scripts.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


_INTERNAL_LINK = re.compile(r'<a href="/(docs|blog)/([a-z0-9-]+)">(.*?)</a>', re.S)


def render_markdown(text: str, live_pages: set[str] | None = None) -> str:
    """Render Markdown. With live_pages ({"/docs/slug", ...}), links to internal pages
    that aren't published become plain text, so visitors never hit a missing page."""
    html = _md.render(text)
    if live_pages is None:
        return html
    return _INTERNAL_LINK.sub(lambda m: m[0] if f"/{m[1]}/{m[2]}" in live_pages else m[3], html)


def internal_links(markdown: str) -> set[str]:
    return set(re.findall(r"\]\((/(?:docs|blog)/[a-z0-9-]+)\)", markdown))


def slugify(text: str) -> str:
    text = re.sub(r"['’]", "", text.lower())  # "children's" -> "childrens"
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80]


def _norm(text: str) -> str:
    """Lowercase, apostrophes dropped, hyphens and runs of spaces as single spaces."""
    text = re.sub(r"['’]", "", text.lower())
    return " ".join(re.sub(r"[-–—]", " ", text).split())


def valid_slug(slug: str) -> bool:
    return bool(_SLUG.match(slug)) and len(slug) <= 80


def plain_text(markdown: str) -> str:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.S)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[#>*_`|\-]+", " ", text)


def word_count(markdown: str) -> int:
    return len(re.findall(r"\b\w+\b", plain_text(markdown)))


def reading_minutes(markdown: str) -> int:
    return max(1, round(word_count(markdown) / 220))


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """`---\\nkey: value\\n---\\nbody` -> ({key: value}, body). Values are single-line strings."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[end + 5 :].lstrip("\n")


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str = ""


def seo_checks(item: dict[str, Any], live_pages: set[str] | None = None) -> list[Check]:
    """On-page SEO checklist for the editor. Advisory: none of these block publishing.

    With live_pages, also flags links to pages that aren't published yet."""
    title, desc = item.get("title") or "", item.get("description") or ""
    body, slug = item.get("body_md") or "", item.get("slug") or ""
    keyword = _norm(item.get("keyword") or "")
    words = word_count(body)
    first_para = _norm(" ".join(plain_text(body).split()[:100]))
    h2 = len(re.findall(r"^## ", body, re.M))
    internal = len(re.findall(r"\]\(/(?:docs|blog)/", body))
    checks = [
        Check("Title is 30-60 characters", 30 <= len(title) <= 60, f"{len(title)} characters"),
        Check(
            "Meta description is 70-160 characters",
            70 <= len(desc) <= 160,
            f"{len(desc)} characters",
        ),
        Check("Focus keyword is set", bool(keyword)),
    ]
    if keyword:
        slug_words = set(slug.split("-"))
        checks += [
            Check("Keyword in the title", keyword in _norm(title)),
            Check("Keyword in the meta description", keyword in _norm(desc)),
            Check("Keyword in the first 100 words", keyword in first_para),
            Check("Keyword in the URL", all(w in slug_words for w in slugify(keyword).split("-"))),
        ]
    minimum = 600 if item.get("type") == "blog" else 300
    checks += [
        Check(f"At least {minimum} words", words >= minimum, f"{words} words"),
        Check("Uses subheadings (##)", h2 >= 2, f"{h2} subheadings"),
        Check("Links to other docs or posts", internal >= 1, f"{internal} internal links"),
    ]
    if live_pages is not None:
        dead = sorted(internal_links(body) - live_pages)
        checks.append(
            Check(
                "Every linked page is published",
                not dead,
                ("not yet: " + ", ".join(dead[:4]) + ("…" if len(dead) > 4 else ""))
                if dead
                else "",
            )
        )
    return checks


def seed_items() -> list[dict[str, str]]:
    """The bundled starter docs and posts."""
    items = []
    root = resources.files("grc_agent.content_seed")
    for type_ in TYPES:
        folder = root.joinpath(type_)
        for entry in sorted(folder.iterdir(), key=lambda p: p.name):
            if not entry.name.endswith(".md"):
                continue
            meta, body = parse_front_matter(entry.read_text("utf-8"))
            items.append(
                {
                    "type": type_,
                    "slug": entry.name[:-3],
                    "title": meta.get("title", entry.name[:-3]),
                    "description": meta.get("description", ""),
                    "keyword": meta.get("keyword", ""),
                    "position": int(meta.get("order", 100)),
                    "body_md": body,
                }
            )
    return items
