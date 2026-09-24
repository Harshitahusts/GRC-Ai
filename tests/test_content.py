import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from helpers import login, post

from grc_agent.content import parse_front_matter, render_markdown, seed_items, seo_checks, slugify
from grc_agent.web.app import create_app


def content_id(client, slug):
    page = client.get("/content").text
    match = re.search(
        rf'href="/content/(\d+)">[^<]*</a><br><span class="muted small">/\w+/{slug}<', page
    )
    assert match, f"{slug} not in the content list"
    return int(match[1])


def publish(client, cid, **overrides):
    edit = client.get(f"/content/{cid}").text
    fields = {
        name: re.search(rf'name="{name}" value="([^"]*)"', edit)[1]
        for name in ("title", "slug", "keyword")
    }
    description = re.search(r'name="description"[^>]*>([^<]*)</textarea>', edit)[1]
    body = re.search(r'name="body_md"[^>]*>(.*?)</textarea>', edit, re.S)[1]
    data = {
        **{k: html.unescape(v) for k, v in fields.items()},
        "description": html.unescape(description),
        "body_md": html.unescape(body),
        "reviewed_by": "Suryansh",
        "action": "publish",
        **overrides,
    }
    return post(client, f"/content/{cid}", data)


# ---- unit


def test_seed_content_is_complete_and_passes_seo_checks():
    items = seed_items()
    docs = [i for i in items if i["type"] == "docs"]
    blogs = [i for i in items if i["type"] == "blog"]
    assert len(docs) == 13 and len(blogs) == 2
    for item in items:
        failed = [c.label for c in seo_checks(item) if not c.ok]
        assert not failed, (item["slug"], failed)


def test_seed_internal_links_all_resolve():
    items = seed_items()
    pages = {f"/{i['type']}/{i['slug']}" for i in items}
    for item in items:
        links = set(re.findall(r"\]\((/(?:docs|blog)/[a-z0-9-]+)\)", item["body_md"]))
        assert links <= pages, (item["slug"], links - pages)


def test_markdown_escapes_html_and_unsafe_links():
    html = render_markdown(
        "<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n| a |\n|---|\n| 1 |"
    )
    assert "<script>" not in html and 'href="javascript' not in html
    assert "<table>" in html


def test_front_matter():
    meta, body = parse_front_matter("---\ntitle: Hello: world\nkeyword: k\n---\n\n# Body")
    assert meta == {"title": "Hello: world", "keyword": "k"} and body == "# Body"
    assert parse_front_matter("no front matter") == ({}, "no front matter")


def test_slugify():
    assert slugify("Children's data: a guide!") == "childrens-data-a-guide"


@pytest.mark.parametrize(
    "item, label, ok",
    [
        ({"title": "x" * 45}, "Title is 30-60 characters", True),
        ({"title": "short"}, "Title is 30-60 characters", False),
        (
            {"keyword": "cross-border data", "title": "Cross border data rules explained"},
            "Keyword in the title",
            True,
        ),
        (
            {"keyword": "rules 2025", "slug": "dpdp-rules-2025-explained"},
            "Keyword in the URL",
            True,
        ),
        ({"keyword": "rules 2025", "slug": "dpdp-rules"}, "Keyword in the URL", False),
    ],
)
def test_seo_checks(item, label, ok):
    checks = {c.label: c.ok for c in seo_checks(item)}
    assert checks[label] is ok


# ---- web


def test_starter_content_is_seeded_as_drafts_and_hidden(client):
    assert client.get("/docs/dpdpa-overview").status_code == 404
    assert "Nothing published yet" in client.get("/docs").text
    assert "dpdpa-overview" not in client.get("/sitemap.xml").text


def test_editor_requires_login(client):
    for path in ["/content", "/content/new", "/content/1"]:
        assert client.get(path, follow_redirects=False).status_code == 303


def test_publishing_needs_a_reviewer(authed):
    cid = content_id(authed, "dpdpa-overview")
    page = publish(authed, cid, reviewed_by="").text
    assert "Add who reviewed this before publishing" in page
    assert authed.get("/docs/dpdpa-overview").status_code == 404


def test_publish_then_public_page_with_seo_tags(authed):
    cid = content_id(authed, "dpdpa-overview")
    assert "Published." in publish(authed, cid).text

    public = TestClient(authed.app)  # a visitor with no session
    page = public.get("/docs/dpdpa-overview")
    assert page.status_code == 200
    html = page.text
    assert "<title>DPDPA 2023 overview: what the law requires · GRC agent</title>" in html
    assert '<meta name="description" content="A plain-English DPDPA overview' in html
    assert '<link rel="canonical" href="http://testserver/docs/dpdpa-overview">' in html
    assert '<meta property="og:type" content="article">' in html
    data = json.loads(
        re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)[1]
    )
    assert data["@type"] == "Article" and data["headline"].startswith("DPDPA 2023 overview")
    assert "<h2>Who the DPDPA applies to</h2>" in html
    assert "Not legal advice" in html
    assert "dpdpa-overview" in public.get("/sitemap.xml").text
    assert "DPDPA 2023 overview" in public.get("/docs").text


def test_unpublish_hides_it_again(authed):
    cid = content_id(authed, "dpdpa-overview")
    publish(authed, cid)
    publish(authed, cid, action="unpublish")
    assert TestClient(authed.app).get("/docs/dpdpa-overview").status_code == 404


def test_create_blog_post_and_escape_html(authed):
    page = post(
        authed,
        "/content",
        {
            "type": "blog",
            "title": "Our first post about DPDPA",
            "slug": "",
            "description": "d" * 80,
            "keyword": "dpdpa",
            "body_md": "## Hi\n\n<img src=x onerror=alert(1)>",
            "reviewed_by": "",
        },
    )
    assert "Draft saved." in page.text
    cid = content_id(authed, "our-first-post-about-dpdpa")
    publish(authed, cid)
    html = TestClient(authed.app).get("/blog/our-first-post-about-dpdpa").text
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert '"@type": "BlogPosting"' in html


def test_duplicate_and_invalid_slugs_are_rejected(authed):
    base = {
        "type": "docs",
        "title": "Another overview",
        "description": "",
        "keyword": "",
        "body_md": "",
    }
    page = post(authed, "/content", {**base, "slug": "dpdpa-overview"})
    assert page.status_code == 400 and "already a docs item" in page.text
    page = post(authed, "/content", {**base, "slug": "Bad Slug!"})
    assert page.status_code == 400 and "lowercase letters" in page.text


def test_robots_and_sitemap(client, monkeypatch):
    monkeypatch.setenv("GRC_PUBLIC_URL", "https://example.in")
    robots = client.get("/robots.txt").text
    assert "Allow: /docs" in robots and "Disallow: /\n" in robots
    assert "Sitemap: https://example.in/sitemap.xml" in robots
    assert client.get("/sitemap.xml").headers["content-type"].startswith("application/xml")


def test_seed_runs_once_and_never_overwrites_edits(authed, tmp_path):
    cid = content_id(authed, "dpdpa-overview")
    publish(authed, cid, title="My edited overview title for DPDPA", slug="my-overview")
    again = create_app(authed.app.state.db_path.parent)  # restart
    fresh = TestClient(again)
    login(fresh)
    listing = fresh.get("/content").text
    assert "My edited overview title for DPDPA" in listing
    assert "/docs/dpdpa-overview<" not in listing  # renamed item isn't re-imported
    assert listing.count("/docs/") == 13


def test_links_to_unpublished_pages_are_plain_text_until_published(authed):
    publish(authed, content_id(authed, "dpdpa-compliance-for-small-businesses"))
    visitor = TestClient(authed.app)
    html = visitor.get("/blog/dpdpa-compliance-for-small-businesses").text
    assert 'href="/docs/dpdpa-penalties"' not in html and "DPDPA penalties" in html

    publish(authed, content_id(authed, "dpdpa-penalties"))
    html = visitor.get("/blog/dpdpa-compliance-for-small-businesses").text
    assert 'href="/docs/dpdpa-penalties"' in html


def test_editor_warns_about_links_to_unpublished_pages(authed):
    page = authed.get(f"/content/{content_id(authed, 'dpdpa-overview')}").text
    assert "Every linked page is published" in page and "not yet: /docs/" in page


def test_render_markdown_unlinks_only_dead_internal_links():
    md = "[a](/docs/live) [b](/docs/dead) [c](https://meity.gov.in)"
    html = render_markdown(md, {"/docs/live"})
    assert '<a href="/docs/live">a</a>' in html
    assert '<a href="/docs/dead">' not in html and " b " in html
    assert 'href="https://meity.gov.in"' in html
