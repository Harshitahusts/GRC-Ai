"""Shared helpers for the web tests."""

import re

PASSWORD = "correct-horse-battery"


def csrf(client, path="/login?as=company"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(client, password=PASSWORD):
    return client.post(
        "/login",
        data={"username": "harshit", "password": password, "csrf": csrf(client)},
        follow_redirects=False,
    )


def post(client, path, data=None, **kwargs):
    return client.post(path, data={**(data or {}), "csrf": csrf(client, "/")}, **kwargs)


def create(client):
    response = post(client, "/engagements", {"client": "Acme Pvt Ltd", "sector": "SaaS"})
    return int(response.url.path.rsplit("/", 1)[1])


ALL_YES = {
    "q_INFO-DATA": "Names, emails",
    "q_CTX-CHILDREN": "no",
    "q_CTX-VENDORS": "yes",
    "q_CTX-FOREIGN": "no",
    **{
        f"q_{q}": "yes"
        for q in [
            "Q-NOTICE",
            "Q-CONSENT",
            "Q-WITHDRAW",
            "Q-SECURITY",
            "Q-BREACH",
            "Q-ERASURE",
            "Q-CONTACT",
            "Q-GRIEVANCE",
            "Q-ACCESS",
            "Q-CORRECTION",
            "Q-VENDOR-CONTRACT",
        ]
    },
}


MEMBER_PASSWORD = "role-test-password"  # noqa: S105


def add_member(app, name, *, org=None, kind="client", team_role="admin", password=MEMBER_PASSWORD):
    """A customer who signs in with a password, in a new organisation (or `org`'s id).
    Returns the organisation id."""
    from grc_agent.web import access, db
    from grc_agent.web.security import hash_password

    with db.connect(app.state.db_path) as c:
        if org is None:
            org = access.new_org(c, f"{name} org", kind, None, "test")
        c.execute(
            "INSERT INTO users (username, password_hash, created_at, role, org_id, team_role) "
            "VALUES (?,?,?,?,?,?)",
            (name, hash_password(password), "2026-01-01", "user", org, team_role),
        )
    return org
