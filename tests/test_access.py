"""Organisations, team roles and the hidden super admin dashboard (web/access.py).

Each customer organisation (a company or a partner) sees only its own engagements, its
people have team roles (Admin / Manager / Viewer), and the platform roles are set only on
the owner's dashboard. These tests sign in as each kind of person and try to reach what
they shouldn't.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient
from helpers import MEMBER_PASSWORD as PW
from helpers import add_member, csrf, post

from grc_agent.web import access
from grc_agent.web import db as webdb


def db(app):
    return webdb.connect(app.state.db_path)


def sign_in(app, name, portal="company"):
    c = TestClient(app)
    token = csrf(c, f"/login?as={portal}")
    r = c.post("/login", data={"username": name, "password": PW, "csrf": token, "portal": portal})
    assert r.status_code == 200 and r.url.path != "/login", r.text[-600:]
    return c


def make(c, name):
    r = post(c, "/engagements", {"client": name, "sector": "SaaS"})
    return int(r.url.path.rsplit("/", 1)[1])


@pytest.fixture
def partners(app, authed):
    """Two partner firms: A (pia admin, pete manager, vik viewer) and B (raj admin)."""
    a = add_member(app, "pia", kind="partner")
    add_member(app, "pete", org=a, team_role="manager")
    add_member(app, "vik", org=a, team_role="viewer")
    add_member(app, "raj", kind="partner")
    pia, raj = sign_in(app, "pia", "partner"), sign_in(app, "raj", "partner")
    return pia, raj, make(pia, "Pia Client Co"), make(raj, "Raj Client Co")


def test_first_account_owns_the_workspace(app):
    with db(app) as conn:
        assert access.role_of(conn, "harshit") == "super_admin"


def test_two_step_login_and_the_right_door(app, partners):
    page = TestClient(app).get("/login").text
    assert "Log in as Company" in page and "Log in as Partner" in page
    assert 'name="password"' not in page
    # A partner at the company door: refused, told where to go, not signed in.
    c = TestClient(app)
    token = csrf(c, "/login?as=company")
    data = {"username": "pia", "password": PW, "csrf": token, "portal": "company"}
    r = c.post("/login", data=data)
    assert r.status_code == 403 and "Use Partner login" in r.text
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    # A wrong password gets the usual message, never which door the account uses.
    r = c.post("/login", data={**data, "password": "nope"})
    assert r.status_code == 401 and "Use Partner login" not in r.text


def test_partners_never_see_each_others_engagements(app, authed, partners):
    pia, raj, pia_eid, raj_eid = partners
    for path in ("", "/intake", "/r/tasks", "/dataflow", "/evidence", "/export.json"):
        assert raj.get(f"/engagements/{pia_eid}{path}").status_code == 404, path
    assert post(raj, f"/engagements/{pia_eid}/r/tasks", {"title": "x"}).status_code == 404
    for page in ("/", "/engagements", "/work", "/dataflows", "/connectors", "/notifications"):
        assert "Pia Client Co" not in raj.get(page).text, page
    listing = authed.get("/engagements").text
    assert "Pia Client Co" in listing and "Raj Client Co" in listing  # staff see all


def test_a_team_shares_its_engagements_and_viewers_only_read(app, partners):
    _, _, pia_eid, _ = partners
    pete, vik = sign_in(app, "pete", "partner"), sign_in(app, "vik", "partner")
    assert "Pia Client Co" in pete.get("/engagements").text
    assert post(pete, f"/engagements/{pia_eid}/r/tasks", {"title": "x"}).status_code == 200
    assert vik.get(f"/engagements/{pia_eid}").status_code == 200
    r = post(vik, f"/engagements/{pia_eid}/r/tasks", {"title": "y"})
    assert r.status_code == 403 and "read-only" in r.text
    # Leads: admins and managers on their own work, never viewers.
    with db(app) as conn:
        assert access.leads(conn, "pete", pia_eid) and not access.leads(conn, "vik", pia_eid)


def test_customers_cannot_open_staff_pages(app, partners):
    pia, *_ = partners
    for path in ("/audit", "/audit.csv", "/data-manager", "/settings/ai"):
        assert pia.get(path).status_code == 403, path
    assert pia.get("/settings/api-keys").status_code == 200  # a partner admin may
    vik = sign_in(app, "vik", "partner")
    assert vik.get("/settings/api-keys").status_code == 403  # a viewer may not


def test_team_page_is_the_organisations_own(app, authed, partners):
    pia, raj, *_ = partners
    page = pia.get("/team").text
    assert "pete" in page and "vik" in page and "raj" not in page
    assert "Super admin" not in page and "14 day" not in page
    # The admin invites a teammate; without email the one-time link is shown once.
    r = post(pia, "/team", {"email": "neha@pia.example", "team_role": "manager"})
    assert 'id="invite-link"' in r.text and "neha" in r.text
    with db(app) as conn:
        neha = access.account(conn, "neha")
        assert neha.org_name == "pia org" and neha.team_role == "manager"
    post(pia, "/team/neha/role", {"team_role": "viewer"})
    with db(app) as conn:
        assert access.account(conn, "neha").team_role == "viewer"
    # Another firm's people can't be touched, and the last admin stays.
    assert post(pia, "/team/raj/role", {"team_role": "viewer"}).status_code == 404
    assert "Keep at least one admin" in post(pia, "/team/pia/role", {"team_role": "viewer"}).text
    post(pia, "/team/neha/remove")
    with db(app) as conn:
        assert access.account(conn, "neha") is None
    # Managers and viewers see the team but can't change it.
    pete = sign_in(app, "pete", "partner")
    assert "Invite a teammate" not in pete.get("/team").text
    assert post(pete, "/team", {"email": "x@pia.example"}).status_code == 403
    # The owner has no customer team: Team & roles takes them to their dashboard.
    assert authed.get("/team", follow_redirects=False).headers["location"] == "/dashboard"


def test_partner_brings_a_clients_person_into_one_engagement(app, partners):
    pia, _, pia_eid, raj_eid = partners
    r = post(pia, f"/engagements/{pia_eid}/access", {"email": "cleo@client.example"})
    assert 'id="invite-link"' in r.text
    with db(app) as conn:  # as if cleo had set her password from the link
        conn.execute(
            "UPDATE users SET password_hash = (SELECT password_hash FROM users "
            "WHERE username = 'pia') WHERE username = 'cleo'"
        )
    cleo = sign_in(app, "cleo")
    assert cleo.get("/", follow_redirects=False).headers["location"] == f"/engagements/{pia_eid}"
    assert cleo.get(f"/engagements/{raj_eid}").status_code == 404
    assert post(cleo, f"/engagements/{pia_eid}/deliver").status_code == 403
    assert cleo.get("/team").status_code == 404  # no organisation of her own
    post(pia, f"/engagements/{pia_eid}/access/cleo/remove")
    assert cleo.get(f"/engagements/{pia_eid}").status_code == 404


def test_a_company_assesses_itself(app):
    add_member(app, "dina", kind="client")
    dina = sign_in(app, "dina")
    eid = make(dina, "Dina Retail")
    with db(app) as conn:
        row = conn.execute(
            "SELECT audience, org_id FROM engagements WHERE id = ?", (eid,)
        ).fetchone()
        assert row["audience"] == "self" and row["org_id"] is not None
        assert access.leads(conn, "dina", eid)


# ---- the hidden super admin dashboard


def test_dashboard_is_hidden_from_everyone_but_the_owner(app, authed, partners):
    pia, *_ = partners
    assert "Super admin dashboard" in authed.get("/dashboard").text
    add_member(app, "ada")
    with db(app) as conn:
        conn.execute("UPDATE users SET role = 'admin', org_id = NULL WHERE username = 'ada'")
    for c in (pia, sign_in(app, "ada")):
        assert c.get("/dashboard").status_code == 404
        assert post(c, "/dashboard/accounts", {"email": "x@example.com"}).status_code == 404
    assert TestClient(app).get("/dashboard", follow_redirects=False).status_code == 404
    for page in ("/", "/engagements"):
        assert 'href="/dashboard"' not in authed.get(page).text
    owner = authed.get("/dashboard").text
    assert "noindex" in owner and 'class="sidebar"' not in owner


def test_owner_sets_up_a_poc_of_any_length(app, authed):
    eid = make(authed, "Prospect Ltd")
    r = post(
        authed,
        "/dashboard/accounts",
        {
            "org": "new",
            "org_name": "Prospect Ltd",
            "kind": "client",
            "poc": "1",
            "poc_days": "45",
            "email": "neha.k@gmail.com",
            "team_role": "manager",
            "engagements": str(eid),
        },
    )
    link = re.search(r'id="invite-link" value="([^"]+)"', r.text).group(1)
    assert "/invite/" in link
    with db(app) as conn:
        neha = access.account(conn, "neha.k")
        assert neha.org_name == "Prospect Ltd" and neha.team_role == "manager"
        assert access.days_left(neha.poc_until) == 45
        assert access.can_see(conn, "neha.k", eid)
        oid = neha.org_id
    # Extend by any number of days, from the current end.
    post(authed, f"/dashboard/orgs/{oid}/poc", {"days": "10"})
    with db(app) as conn:
        assert access.days_left(access.account(conn, "neha.k").poc_until) == 55
    assert "1 to 365" in post(authed, f"/dashboard/orgs/{oid}/poc", {"days": "0"}).text
    post(authed, f"/dashboard/orgs/{oid}/convert")
    with db(app) as conn:
        assert access.account(conn, "neha.k").poc_until == ""


def test_an_ended_poc_signs_its_people_out(app, authed, partners):
    pia, *_ = partners
    with db(app) as conn:
        oid = access.account(conn, "pia").org_id
    post(authed, f"/dashboard/orgs/{oid}/end")
    r = pia.get("/engagements", follow_redirects=True)
    assert r.url.path == "/login" and "POC has ended" in r.text
    c = TestClient(app)
    token = csrf(c, "/login?as=partner")
    data = {"username": "pete", "password": PW, "csrf": token, "portal": "partner"}
    r = c.post("/login", data=data)
    assert r.status_code == 403 and "POC has ended" in r.text
    post(authed, f"/dashboard/orgs/{oid}/poc", {"days": "7"})
    assert sign_in(app, "pete", "partner").get("/engagements").status_code == 200


def test_owner_manages_people_and_platform_roles(app, authed, partners):
    r = post(authed, "/dashboard/accounts", {"org": "staff", "email": "ops@grc-flow.example"})
    assert "Send them the invite link" in r.text
    with db(app) as conn:
        assert access.role_of(conn, "ops") == "admin"
    post(authed, "/dashboard/people/vik/team-role", {"team_role": "manager"})
    with db(app) as conn:
        assert access.account(conn, "vik").team_role == "manager"
    r = post(authed, "/dashboard/people/harshit/role", {"role": "user"})
    assert "Keep at least one super admin" in r.text
    post(authed, "/dashboard/people/vik/remove")
    with db(app) as conn:
        assert access.account(conn, "vik") is None


def _rpc(c, key, method, params=None):
    body = {"jsonrpc": "2.0", "method": method, "id": 1, "params": params or {}}
    return c.post("/mcp", content=json.dumps(body), headers={"Authorization": f"Bearer {key}"})


def test_a_partners_mcp_key_reads_only_its_engagements(app, partners):
    pia, _, pia_eid, raj_eid = partners
    page = post(pia, "/settings/api-keys", {"name": "Claude"}).text
    key = re.search(r'id="new-key">(grcf_[^<]+)<', page).group(1)
    r = _rpc(pia, key, "tools/call", {"name": "list_engagements", "arguments": {}})
    text = r.json()["result"]["content"][0]["text"]
    assert "Pia Client Co" in text and "Raj Client Co" not in text
    args = {"name": "get_engagement", "arguments": {"engagement_id": raj_eid}}
    assert _rpc(pia, key, "tools/call", args).json()["result"]["isError"]
    with db(app) as conn:  # demoted to viewer: the key stops working
        conn.execute("UPDATE users SET team_role = 'viewer' WHERE username = 'pia'")
    assert _rpc(pia, key, "ping").status_code == 401


def test_older_roles_become_organisations(app, authed):
    eid = make(authed, "Legacy Co")
    with db(app) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role, expires_at) VALUES "
            "('mem', 'x', '2026-01-01', 'member', ''), ('vie', 'x', '2026-01-01', 'viewer', ''), "
            "('tri', 'x', '2026-01-01', 'trial', '2030-01-01T00:00:00+00:00')"
        )
        conn.execute("UPDATE engagements SET created_by = 'mem' WHERE id = ?", (eid,))
    webdb.init_db(app.state.db_path)  # what a restart does
    with db(app) as conn:
        mem, vie, tri = (access.account(conn, n) for n in ("mem", "vie", "tri"))
        assert mem.role == "user" and mem.partner and mem.team_role == "admin"
        assert vie.role == "user" and not vie.partner
        assert tri.poc_until.startswith("2030-01-01")
        assert access.can_see(conn, "mem", eid) and access.can_see(conn, "vie", eid)
        assert conn.execute("SELECT org_id FROM engagements WHERE id = ?", (eid,)).fetchone()[0]
