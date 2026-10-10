"""Who can see and do what: the five roles and the clients each person can open.

GRC Flow is sold two ways. A company uses it for its own DPDP compliance (a **client**),
and a consultancy or MSSP uses it to run DPDP work for many companies (a **partner**).
Both can share one workspace, so every client (engagement) is fenced off:

- **super_admin** and **admin** see every client. Only they manage the team, the AI
  provider, the data manager and the audit log; only a super admin manages admins.
- **partner** sees the clients they created or were given, and can bring each client's
  own people in. Partners can make API keys for the MCP server, which then reads only
  their clients.
- **client** sees their own company only: the engagements they created for themselves
  or were given.
- **trial** is a client account that stops working on a date, with one engagement and
  no API keys.

The fence is enforced in one place for pages (the middleware in app.py checks every
/engagements/<id> address) and by ``visible_ids`` for anything that lists clients.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from grc_agent.web import db

ROLES = {
    "super_admin": "Super admin: owns the workspace. Everything, including admins",
    "admin": "Admin: every client, the team and the settings",
    "partner": "Partner: their own clients, client invites, API keys and MCP",
    "client": "Client: their own company's DPDP work only",
    "trial": "Trial: one engagement until the trial ends; no API keys",
}
# Roles an admin may hand out; only a super admin can make or change another admin.
STAFF = {"super_admin", "admin"}
API_ROLES = {"super_admin", "admin", "partner"}  # may hold API keys and use MCP
TRIAL_DAYS = 14
TRIAL_ENGAGEMENTS = 1

ENGAGEMENT_PATH = re.compile(r"^/engagements/(\d+)(?:/|\.|$)")
# Pages for staff only, and pages for partners and staff.
STAFF_PATHS = re.compile(
    r"^/(team(/|$)|settings/(ai|github-app)(/|$)|data-manager$|audit(\.csv)?$)"
)
# The MCP endpoint itself is checked by its API key's owner (mcp_views), not the session.
API_PATHS = re.compile(r"^/settings/api-keys(/|$)")


def role_of(conn: sqlite3.Connection, username: str) -> str:
    row = conn.execute(
        "SELECT role FROM users WHERE LOWER(username) = LOWER(?)", (username,)
    ).fetchone()
    return row["role"] if row else ""


def is_staff(role: str) -> bool:
    return role in STAFF


def visible_ids(
    conn: sqlite3.Connection, username: str, role: str | None = None
) -> set[int] | None:
    """The engagement ids this person may open, or None for every engagement (staff)."""
    role = role if role is not None else role_of(conn, username)
    if is_staff(role):
        return None
    rows = conn.execute(
        "SELECT id FROM engagements WHERE LOWER(created_by) = LOWER(?) "
        "UNION SELECT engagement_id FROM engagement_access WHERE LOWER(username) = LOWER(?)",
        (username, username),
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


def leads(conn: sqlite3.Connection, username: str, eid: int, role: str | None = None) -> bool:
    """May this person make the lead's calls on an engagement: approve 'not applicable',
    reopen a delivery, give others access? Staff and partners on their clients; a
    client only on an engagement they set up for their own company."""
    role = role if role is not None else role_of(conn, username)
    if is_staff(role):
        return True
    if not can_see(conn, username, eid, role):
        return False
    if role == "partner":
        return True
    if role == "client":
        row = conn.execute("SELECT created_by FROM engagements WHERE id = ?", (eid,)).fetchone()
        return bool(row) and row["created_by"].lower() == username.lower()
    return False


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
    return conn.execute(
        "SELECT a.username, a.granted_by, a.granted_at, u.role, u.email FROM engagement_access a "
        "LEFT JOIN users u ON LOWER(u.username) = LOWER(a.username) "
        "WHERE a.engagement_id = ? ORDER BY a.username",
        (eid,),
    ).fetchall()


def assignable(conn: sqlite3.Connection, eid: int) -> list[str]:
    """Usernames who can open an engagement, so work can be assigned to them: staff, the
    person who created it, and those given access. Nobody else's name is shown."""
    rows = conn.execute(
        "SELECT username FROM users WHERE role IN ('super_admin', 'admin') "
        "UNION SELECT created_by FROM engagements WHERE id = ? "
        "UNION SELECT username FROM engagement_access WHERE engagement_id = ?",
        (eid, eid),
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


def trial_end(days: int = TRIAL_DAYS) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        end = datetime.fromisoformat(expires_at)
    except ValueError:
        return True  # an unreadable date never keeps an account open
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= end


def days_left(expires_at: str | None) -> int | None:
    if not expires_at:
        return None
    try:
        end = datetime.fromisoformat(expires_at)
    except ValueError:
        return 0
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    seconds = (end - datetime.now(timezone.utc)).total_seconds()
    return max(0, int((seconds + 86399) // 86400))


def path_allowed(path: str, role: str) -> bool:
    """The page-level gates: staff pages and API pages."""
    if STAFF_PATHS.match(path):
        return is_staff(role)
    if API_PATHS.match(path):
        return role in API_ROLES
    return True


def may_create(conn: sqlite3.Connection, username: str, role: str) -> str:
    """Why this person can't start a new engagement, or '' if they can."""
    if role == "trial":
        made = conn.execute(
            "SELECT COUNT(*) FROM engagements WHERE LOWER(created_by) = LOWER(?)", (username,)
        ).fetchone()[0]
        if made >= TRIAL_ENGAGEMENTS:
            return "A trial includes one engagement. Talk to us to add more."
    return ""
