"""Login and dashboard UX, the search palette and the Data manager."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import ALL_YES, create, csrf, post

from grc_agent.web import datamanager, db

TEMPLATES = Path(__file__).parents[1] / "src" / "grc_agent" / "web" / "templates"


def test_no_placeholder_buttons_for_features_that_do_not_exist():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"data-soon|soon-tag|Coming next", text), path.name


def test_login_page_has_the_new_flow(client):
    page = client.get("/login").text
    for text in ('id="reveal"', "Caps Lock is on", "Ask its admin or partner", 'href="/forgot"'):
        assert text in page
    assert "Continue with Google" not in page  # not configured in tests
    assert 'id="palette"' not in page  # the search palette is for signed-in users only
    assert 'name="portal" value="client"' in page and 'name="portal" value="partner"' in page


def test_failed_login_keeps_the_username(client):
    token = csrf(client)
    page = client.post(
        "/login", data={"username": "harshit", "password": "nope", "csrf": token}
    ).text
    assert 'value="harshit"' in page and 'aria-invalid="true"' in page


def test_app_shell_has_search_menu_and_shortcuts(authed):
    page = authed.get("/").text
    assert 'id="palette"' in page and 'id="shortcuts"' in page
    assert 'href="/data-manager"' in page and "Search all records for" not in page
    assert "Getting started" in page and "0 of 4 done" in page


def test_getting_started_tracks_progress(authed):
    page = authed.get("/").text
    assert "Getting started" in page
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", {**ALL_YES, "action": "submit"})
    page = authed.get("/").text
    assert "2 of 4 done" in page
    assert page.count('class="is-done"') == 2


def test_dashboard_quick_actions_and_store_line(authed):
    page = authed.get("/").text
    assert 'href="/team"' in page and "Add a teammate" in page and "Invite teammate" not in page
    assert "Data store:" in page and "Data manager" in page


def test_data_manager_page(authed):
    create(authed)
    page = authed.get("/data-manager").text
    assert "Data catalogue" in page and "Watch list" in page
    assert "<code>engagements</code>" in page and "<code>audit_log</code>" in page
    assert "Integrity: ok" in page and "Back up regularly" in page
    assert "Back up now" not in page and "Purge expired" not in page


def test_data_manager_needs_login(client):
    response = client.get("/data-manager", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_report_flags_expired_rows_and_unknown_tables(tmp_path):
    path = tmp_path / "grc.db"
    db.init_db(path)
    with db.connect(path) as conn:
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        conn.execute(
            "INSERT INTO audit_log (at, username, action) VALUES (?,?,?)", (old, "a", "login")
        )
        conn.execute(
            "INSERT INTO audit_log (at, username, action) VALUES (?,?,?)", (db.now(), "a", "x")
        )
        conn.execute("CREATE TABLE scratch (id INTEGER)")
        r = datamanager.report(conn, path)
    tables = {t.name: t for t in r["tables"]}
    assert tables["audit_log"].rows == 2 and tables["audit_log"].expired == 1
    assert tables["audit_log"].personal and not tables["scratch"].known
    titles = [w.title for w in r["watch"]]
    assert "1 audit_log row past retention" in titles
    assert "Uncatalogued table: scratch" in titles
    assert not r["healthy"]  # an uncatalogued table is serious
    assert set(datamanager.CATALOG) == set(tables) - {"scratch"}  # every real table catalogued


def test_human_size():
    assert datamanager.human_size(512) == "512 B"
    assert datamanager.human_size(2048) == "2.0 KB"
    assert datamanager.human_size(5 * 1024 * 1024) == "5.0 MB"


def test_removed_features_are_gone(client, authed):
    for path in ("/docs", "/blog", "/sitemap.xml", "/content", "/kpis"):
        assert authed.get(path).status_code == 404, path
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and "Disallow: /" in robots.text
    assert "Docs &amp; blog" not in authed.get("/").text
