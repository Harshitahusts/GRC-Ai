"""The five roles and the fence around each client (web/access.py).

A partner runs DPDP work for its own clients and must never see another partner's; a
client sees only their own company; a trial ends on its date. These tests sign in as
each kind of account and try to reach what they shouldn't.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient
from helpers import csrf, post

from grc_agent.web import access
from grc_agent.web import db as webdb

PW = "role-test-password"


def db(app):
    return webdb.connect(app.state.db_path)


def add(authed, name, role):
    r = post(authed, "/team", {"username": name, "password": PW, "role": role})
    assert f"Added {name}" in r.text, r.text[-400:]


def sign_in(app, name):
    c = TestClient(app)
    token = csrf(c, "/login")
    r = c.post("/login", data={"username": name, "password": PW, "csrf": token})
    assert r.status_code == 200, name
    return c


def make(c, client_name):
    r = post(c, "/engagements", {"client": client_name, "sector": "SaaS"})
    return int(r.url.path.rsplit("/", 1)[1])


@pytest.fixture
def two_partners(app, authed):
    add(authed, "pia", "partner")
    add(authed, "raj", "partner")
    pia, raj = sign_in(app, "pia"), sign_in(app, "raj")
    return pia, raj, make(pia, "Pia Client Co"), make(raj, "Raj Client Co")


def test_first_account_owns_the_workspace(app):
    with db(app) as conn:
        assert access.role_of(conn, "harshit") == "super_admin"


def test_partners_never_see_each_others_clients(app, authed, two_partners):
    pia, raj, pia_eid, raj_eid = two_partners
    assert pia.get(f"/engagements/{pia_eid}").status_code == 200
    for path in ("", "/intake", "/r/tasks", "/dataflow", "/evidence", "/export.json"):
        assert raj.get(f"/engagements/{pia_eid}{path}").status_code == 404, path
    assert post(raj, f"/engagements/{pia_eid}/r/tasks", {"title": "x"}).status_code == 404
    for page in ("/", "/engagements", "/work", "/dataflows", "/connectors", "/notifications"):
        text = raj.get(page).text
        assert "Pia Client Co" not in text, page
    assert "Raj Client Co" in raj.get("/engagements").text
    # Staff see both.
    listing = authed.get("/engagements").text
    assert "Pia Client Co" in listing and "Raj Client Co" in listing


def test_partners_cannot_open_staff_pages_but_can_use_api_keys(app, two_partners):
    pia, *_ = two_partners
    for path in ("/team", "/audit", "/audit.csv", "/data-manager", "/settings/ai"):
        assert pia.get(path).status_code == 403, path
    assert pia.get("/settings/api-keys").status_code == 200
    page = pia.get("/").text
    assert 'href="/team"' not in page and 'href="/settings/api-keys"' in page


def test_partner_invites_a_client_who_sees_only_that_engagement(app, authed, two_partners):
    pia, _, pia_eid, raj_eid = two_partners
    r = post(pia, f"/engagements/{pia_eid}/access", {"username": "cleo", "password": PW})
    assert "Added cleo as Client" in r.text
    with db(app) as conn:
        assert access.role_of(conn, "cleo") == "client"
    cleo = sign_in(app, "cleo")
    # One engagement: the dashboard opens it directly.
    assert cleo.get("/", follow_redirects=False).headers["location"] == f"/engagements/{pia_eid}"
    assert cleo.get(f"/engagements/{pia_eid}/intake").status_code == 200
    assert cleo.get(f"/engagements/{raj_eid}").status_code == 404
    for path in ("/team", "/settings/api-keys"):
        assert cleo.get(path).status_code == 403, path
    # A client can't make the lead's calls on a partner's engagement, or invite others.
    assert post(cleo, f"/engagements/{pia_eid}/deliver").status_code == 403
    assert post(cleo, f"/engagements/{pia_eid}/access", {"username": "x9"}).status_code == 403
    # The partner removes her; she loses it.
    post(pia, f"/engagements/{pia_eid}/access/cleo/remove")
    assert cleo.get(f"/engagements/{pia_eid}").status_code == 404


def test_partner_cannot_manage_access_to_someone_elses_client(app, two_partners):
    _, raj, pia_eid, _ = two_partners
    r = post(raj, f"/engagements/{pia_eid}/access", {"username": "sneaky", "password": PW})
    assert r.status_code == 404
    with db(app) as conn:
        assert access.role_of(conn, "sneaky") == ""


def test_admin_cannot_create_or_demote_staff(app, authed):
    add(authed, "ada", "admin")
    ada = sign_in(app, "ada")
    r = post(ada, "/team", {"username": "evil", "password": PW, "role": "super_admin"})
    assert r.status_code == 403
    assert "Only a super admin" in post(ada, "/team/harshit/role", {"role": "client"}).text
    add(ada, "pat", "partner")  # but non-staff roles are fine
    with db(app) as conn:
        assert access.role_of(conn, "harshit") == "super_admin"


def test_direct_client_runs_their_own_company(app, authed):
    add(authed, "dina", "client")
    dina = sign_in(app, "dina")
    eid = make(dina, "Dina Retail")
    with db(app) as conn:
        assert (
            conn.execute("SELECT audience FROM engagements WHERE id = ?", (eid,)).fetchone()[0]
            == "self"
        )
        assert access.leads(conn, "dina", eid)


def test_trial_gets_one_engagement_and_ends(app, authed):
    add(authed, "tara", "trial")
    tara = sign_in(app, "tara")
    assert "Free trial: 14 days left" in tara.get("/engagements").text
    make(tara, "Tara Co")
    r = post(tara, "/engagements", {"client": "Second Co", "sector": "SaaS"})
    assert "A trial includes one engagement" in r.text
    assert tara.get("/settings/api-keys").status_code == 403
    with db(app) as conn:
        conn.execute(
            "UPDATE users SET expires_at = '2020-01-01T00:00:00+00:00' WHERE username = 'tara'"
        )
    r = tara.get("/engagements", follow_redirects=True)
    assert "trial has ended" in r.text and r.url.path == "/login"
    again = TestClient(app)
    token = csrf(again, "/login")
    r = again.post("/login", data={"username": "tara", "password": PW, "csrf": token})
    assert r.status_code == 403 and "trial has ended" in r.text
    # An admin extends it.
    post(authed, "/team/tara/extend")
    assert sign_in(app, "tara").get("/engagements").status_code == 200


def _rpc(c, key, method, params=None):
    body = {"jsonrpc": "2.0", "method": method, "id": 1, "params": params or {}}
    return c.post("/mcp", content=json.dumps(body), headers={"Authorization": f"Bearer {key}"})


def test_a_partners_mcp_key_reads_only_their_clients(app, two_partners):
    pia, _, pia_eid, raj_eid = two_partners
    page = post(pia, "/settings/api-keys", {"name": "Claude"}).text
    key = re.search(r'id="new-key">(grcf_[^<]+)<', page).group(1)
    r = _rpc(pia, key, "tools/call", {"name": "list_engagements", "arguments": {}})
    text = r.json()["result"]["content"][0]["text"]
    assert "Pia Client Co" in text and "Raj Client Co" not in text
    r = _rpc(
        pia, key, "tools/call", {"name": "get_engagement", "arguments": {"engagement_id": raj_eid}}
    )
    assert r.json()["result"]["isError"]
    # If the partner becomes a client, the key stops working.
    with db(app) as conn:
        conn.execute("UPDATE users SET role = 'client' WHERE username = 'pia'")
    assert _rpc(pia, key, "ping").status_code == 401


def test_old_roles_upgrade_and_keep_what_they_saw(app, authed):
    eid = make(authed, "Legacy Co")
    with db(app) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role) "
            "VALUES ('mem', 'x', '2026-01-01', 'member'), ('vie', 'x', '2026-01-01', 'viewer')"
        )
    webdb.init_db(app.state.db_path)  # what a restart does
    with db(app) as conn:
        assert access.role_of(conn, "mem") == "partner"
        assert access.role_of(conn, "vie") == "client"
        assert access.can_see(conn, "mem", eid) and access.can_see(conn, "vie", eid)


# ---- the super admin console at /dashboard


def test_only_a_super_admin_opens_the_console(app, authed):
    assert "Super admin dashboard" in authed.get("/dashboard").text
    add(authed, "ada", "admin")
    add(authed, "pat", "partner")
    for name in ("ada", "pat"):
        c = sign_in(app, name)
        # It doesn't exist for them: "not found", not "forbidden".
        assert c.get("/dashboard").status_code == 404, name
        assert post(c, "/dashboard/invite", {"email": "x@example.com"}).status_code == 404
    # Nothing in the tool links to it, even for the super admin; signed out it's not found.
    for page in ("/", "/engagements", "/team"):
        assert 'href="/dashboard"' not in authed.get(page).text
    assert TestClient(app).get("/dashboard", follow_redirects=False).status_code == 404
    assert "noindex" in authed.get("/dashboard").text
    assert 'class="sidebar"' not in authed.get("/dashboard").text


def test_console_invites_a_poc_with_access_and_shows_the_link(app, authed):
    eid = make(authed, "Prospect Ltd")
    r = post(
        authed,
        "/dashboard/invite",
        {"email": "neha.k@gmail.com", "role": "trial", "days": "30", "engagements": str(eid)},
    )
    # No email service in tests: the one-time link is shown to share by hand.
    link = re.search(r'id="invite-link" value="([^"]+)"', r.text).group(1)
    assert "/invite/" in link and "neha.k" in r.text
    with db(app) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = 'neha.k'").fetchone()
        assert row["role"] == "trial" and row["email"] == "neha.k@gmail.com"
        assert access.days_left(row["expires_at"]) == 30
        assert access.can_see(conn, "neha.k", eid)
    # The link sets a password and lets them in, straight to their client.
    c = TestClient(app)
    path = link.split("://", 1)[1].split("/", 1)[1]
    token = csrf(c, "/" + path)
    c.post("/" + path, data={"password": PW, "confirm": PW, "csrf": token})
    c2 = sign_in(app, "neha.k")
    assert c2.get("/", follow_redirects=False).headers["location"] == f"/engagements/{eid}"
    # The same email can't be invited twice under another name.
    r = post(authed, "/dashboard/invite", {"email": "neha.k@gmail.com", "role": "client"})
    assert "neha.k2" not in r.text


def test_console_tracks_extends_converts_and_ends_pocs(app, authed):
    post(authed, "/dashboard/invite", {"email": "soon@example.com", "role": "trial", "days": "7"})
    post(authed, "/dashboard/invite", {"email": "later@example.com", "role": "trial", "days": "60"})
    page = authed.get("/dashboard").text
    pocs = page.split('id="pocs"')[1].split('id="invite"')[0]
    assert pocs.index("soon") < pocs.index("later")  # ending soonest first
    assert "7 days left" in pocs
    post(authed, "/dashboard/poc/soon/extend", {"days": "14"})
    with db(app) as conn:
        assert access.days_left(access_end(conn, "soon")) == 21
    post(authed, "/dashboard/poc/later/end")
    with db(app) as conn:
        assert access.expired(access_end(conn, "later"))
    post(authed, "/dashboard/poc/soon/convert")
    with db(app) as conn:
        assert access.role_of(conn, "soon") == "client" and access_end(conn, "soon") == ""


def test_console_gives_and_removes_access(app, authed):
    a, b = make(authed, "Alpha Co"), make(authed, "Beta Co")
    add(authed, "pat", "partner")
    post(authed, "/dashboard/people/pat/grant", {"engagements": str(a)})
    pat = sign_in(app, "pat")
    assert pat.get(f"/engagements/{a}").status_code == 200
    assert pat.get(f"/engagements/{b}").status_code == 404
    post(authed, f"/dashboard/people/pat/revoke/{a}")
    assert pat.get(f"/engagements/{a}").status_code == 404
    # Role changes from the console, and the owner can't remove the last super admin.
    post(authed, "/dashboard/people/pat/role", {"role": "admin"})
    assert pat.get(f"/engagements/{b}").status_code == 200
    r = post(authed, "/dashboard/people/harshit/role", {"role": "client"})
    assert "Keep at least one super admin" in r.text


def access_end(conn, name):
    return conn.execute("SELECT expires_at FROM users WHERE username = ?", (name,)).fetchone()[0]
