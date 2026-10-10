"""Who can see and do what: organisations, their teams, and GRC Flow's own staff.

GRC Flow is sold two ways: a company uses it for its own DPDP compliance (a **client**
organisation), and a consultancy or MSSP runs DPDP work for many companies (a **partner**
organisation). Both share one workspace, so every engagement is fenced off.

Two layers, deliberately separate:

- **Platform roles** (hidden, set only on the super admin dashboard): ``super_admin``
  owns the workspace; ``admin`` is GRC Flow staff and sees every engagement; ``user`` is
  everyone else. Nobody outside the platform team ever sees these.
- **Team roles** inside an organisation (shown on its Team & roles page, managed by its
  own admins): ``admin`` runs the team, ``manager`` does the work, ``viewer`` reads.

A user sees their organisation's engagements, the ones they created, and any they were
given (a partner bringing a client's people in). A POC is an organisation with an end
date (``poc_until``); its people are signed out once it passes.

The fence is enforced in one place for pages (the middleware in app.py checks every
/engagements/<id> address) and by ``visible_ids`` for anything that lists engagements.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from grc_agent.web import db

PLATFORM_ROLES = {
    "super_admin": "Super admin: owns the workspace",
    "admin": "GRC Flow admin: every client, staff settings",
    "user": "Customer: their organisation only",
}
STAFF = {"super_admin", "admin"}
TEAM_ROLES = {
    "admin": "Admin: everything, plus the team",
    "manager": "Manager: does the work and signs off",
    "viewer": "Viewer: reads everything, changes nothing",
}
ORG_KINDS = {"client": "Company", "partner": "Partner"}

ENGAGEMENT_PATH = re.compile(r"^/engagements/(\d+)(?:/|\.|$)")
OWNER_PATHS = re.compile(r"^/dashboard(/|$)")  # the hidden super admin dashboard
STAFF_PATHS = re.compile(r"^/(settings/(ai|github-app)(/|$)|data-manager$|audit(\.csv)?$)")
# The MCP endpoint itself is checked by its API key's owner (mcp_views), not the session.
API_PATHS = re.compile(r"^/settings/api-keys(/|$)")


@dataclass(frozen=True)
class Account:
    username: str
    role: str  # platform role
    org_id: int | None
    org_name: str
    org_kind: str  # 'client', 'partner' or '' (staff, or a guest without an organisation)
    team_role: str
    poc_until: str

    @property
    def staff(self) -> bool:
        return self.role in STAFF

    @property
    def viewer(self) -> bool:
        return not self.staff and self.team_role == "viewer"

    @property
    def partner(self) -> bool:
        return self.org_kind == "partner"

    @property
    def team_admin(self) -> bool:
        return not self.staff and self.org_id is not None and self.team_role == "admin"

    @property
    def can_api(self) -> bool:
        """API keys and MCP: staff, and a partner's admins and managers."""
        return self.staff or (self.partner and self.team_role in ("admin", "manager"))

    @property
    def poc_ended(self) -> bool:
        return not self.staff and expired(self.poc_until)


def account(conn: sqlite3.Connection, username: str) -> Account | None:
    row = conn.execute(
        "SELECT u.username, u.role, u.org_id, u.team_role, o.name AS org_name, o.kind, "
        "o.poc_until FROM users u LEFT JOIN orgs o ON o.id = u.org_id "
        "WHERE LOWER(u.username) = LOWER(?)",
        (username,),
    ).fetchone()
    if row is None:
        return None
    return Account(
        username=row["username"],
        role=row["role"],
        org_id=row["org_id"],
        org_name=row["org_name"] or "",
        org_kind=row["kind"] or "",
        team_role=row["team_role"] or "admin",
        poc_until=row["poc_until"] or "",
    )


def role_of(conn: sqlite3.Connection, username: str) -> str:
    acct = account(conn, username)
    return acct.role if acct else ""


def is_staff(role: str) -> bool:
    return role in STAFF


def visible_ids(
    conn: sqlite3.Connection, username: str, _role: str | None = None
) -> set[int] | None:
    """The engagement ids this person may open, or None for every engagement (staff)."""
    acct = account(conn, username)
    if acct is None:
        return set()
    if acct.staff:
        return None
    rows = conn.execute(
        "SELECT id FROM engagements WHERE LOWER(created_by) = LOWER(?) OR org_id = ? "
        "UNION SELECT engagement_id FROM engagement_access WHERE LOWER(username) = LOWER(?)",
        (username, acct.org_id if acct.org_id is not None else -1, username),
    ).fetchall()
    return {int(r[0]) for r in rows}


def can_see(conn: sqlite3.Connection, username: str, eid: int, role: str | None = None) -> bool:
    ids = visible_ids(conn, username, role)
    return ids is None or eid in ids


def only(rows, ids: set[int] | None, key: str = "engagement_id") -> list:
    """Keep the rows that belong to a visible engagement (all of them for staff)."""
    if ids is None:
        return list(rows)
    return [r for r in rows if r[key] is not None and int(r[key]) in ids]


def leads(conn: sqlite3.Connection, username: str, eid: int, _role: str | None = None) -> bool:
    """May this person make the lead's calls on an engagement: deliver or sign off, reopen,
    approve 'not applicable', give others access? Staff; an organisation's admins and
    managers on its own engagements; whoever set the engagement up. Not viewers, and not
    people a partner brought in to one engagement."""
    acct = account(conn, username)
    if acct is None:
        return False
    if acct.staff:
        return True
    if acct.viewer or not can_see(conn, username, eid):
        return False
    row = conn.execute("SELECT created_by, org_id FROM engagements WHERE id = ?", (eid,)).fetchone()
    if row is None:
        return False
    if acct.org_id is not None and row["org_id"] == acct.org_id:
        return acct.team_role in ("admin", "manager")
    return row["created_by"].lower() == username.lower()


def grant(conn: sqlite3.Connection, eid: int, username: str, by: str) -> bool:
    """Give someone access to an engagement. False if they already had it."""
    if conn.execute(
        "SELECT 1 FROM engagement_access WHERE engagement_id = ? AND LOWER(username) = LOWER(?)",
        (eid, username),
    ).fetchone():
        return False
    conn.execute(
        "INSERT INTO engagement_access (engagement_id, username, granted_by, granted_at) "
        "VALUES (?,?,?,?)",
        (eid, username, by, db.now()),
    )
    return True


def revoke(conn: sqlite3.Connection, eid: int, username: str) -> None:
    conn.execute(
        "DELETE FROM engagement_access WHERE engagement_id = ? AND LOWER(username) = LOWER(?)",
        (eid, username),
    )


def people(conn: sqlite3.Connection, eid: int) -> list[sqlite3.Row]:
    """People given one engagement on top of its organisation's own team."""
    return conn.execute(
        "SELECT a.username, a.granted_by, a.granted_at, u.team_role, u.email "
        "FROM engagement_access a LEFT JOIN users u ON LOWER(u.username) = LOWER(a.username) "
        "WHERE a.engagement_id = ? ORDER BY a.username",
        (eid,),
    ).fetchall()


def assignable(conn: sqlite3.Connection, eid: int) -> list[str]:
    """Usernames who can open an engagement, so work can be assigned to them: staff, its
    organisation's team, the person who created it, and those given access."""
    rows = conn.execute(
        "SELECT username FROM users WHERE role IN ('super_admin', 'admin') "
        "UNION SELECT u.username FROM users u JOIN engagements e ON e.org_id = u.org_id "
        "WHERE e.id = ? "
        "UNION SELECT created_by FROM engagements WHERE id = ? "
        "UNION SELECT username FROM engagement_access WHERE engagement_id = ?",
        (eid, eid, eid),
    ).fetchall()
    return sorted({r[0] for r in rows}, key=str.lower)


def colleagues(conn: sqlite3.Connection, username: str, ids: set[int] | None) -> list[str]:
    """Usernames to show in filters: everyone for staff, else the people on the
    engagements this person can open."""
    if ids is None:
        return [r[0] for r in conn.execute("SELECT username FROM users ORDER BY username")]
    names = {username}
    for eid in ids:
        names.update(assignable(conn, eid))
    return sorted(names, key=str.lower)


def end_after(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def expired(until: str | None) -> bool:
    if not until:
        return False
    try:
        end = datetime.fromisoformat(until)
    except ValueError:
        return True  # an unreadable date never keeps an account open
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= end


def days_left(until: str | None) -> int | None:
    if not until:
        return None
    try:
        end = datetime.fromisoformat(until)
    except ValueError:
        return 0
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    seconds = (end - datetime.now(timezone.utc)).total_seconds()
    return max(0, int((seconds + 86399) // 86400))


def path_allowed(path: str, acct: Account) -> bool:
    """The page-level gates: staff pages and API pages (the owner's dashboard is handled
    separately, so it can answer 'not found')."""
    if STAFF_PATHS.match(path):
        return acct.staff
    if API_PATHS.match(path):
        return acct.can_api
    return True


def new_org(conn: sqlite3.Connection, name: str, kind: str, poc_days: int | None, by: str) -> int:
    cur = conn.execute(
        "INSERT INTO orgs (name, kind, poc_until, created_by, created_at) VALUES (?,?,?,?,?)",
        (name, kind, end_after(poc_days) if poc_days else "", by, db.now()),
    )
    return int(cur.lastrowid)
