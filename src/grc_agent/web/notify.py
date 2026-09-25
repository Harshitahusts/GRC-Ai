"""In-app notifications, built from the audit log.

Every action the app records in audit_log passes through from_audit(). Actions
worth telling someone about become a notification with a level (info, good,
warning, serious, critical), a title and a link. Routine noise (logins, draft
saves, KPI edits) is only audited. The person who did something sees their own
notification as already read, so the unread count shows what others did and
what the system found.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

LEVELS = ("critical", "serious", "warning", "good", "info")
CATEGORIES = {
    "engagement": "Engagements",
    "assessment": "Assessment",
    "documents": "Documents",
    "delivery": "Delivery",
    "connectors": "Connectors",
    "security": "Security",
    "content": "Docs & blog",
}


def _client(conn: sqlite3.Connection, eid: int | None) -> tuple[str, sqlite3.Row | None]:
    if eid is None:
        return "", None
    row = conn.execute("SELECT * FROM engagements WHERE id = ?", (eid,)).fetchone()
    return (row["client"] if row else f"ENG-{eid:03d}"), row


def _connector_name(cid: str) -> str:
    from grc_agent.connectors import BY_ID

    return BY_ID[cid].name if cid in BY_ID else cid


def _rule(
    conn: sqlite3.Connection, actor: str, action: str, eid: int | None, d: dict[str, Any], raw: str
) -> tuple[str, str, str, str] | None:
    """(category, level, title, link) for an audited action, or None to stay quiet."""
    client, eng = _client(conn, eid)
    base = f"/engagements/{eid}" if eid else ""
    if action == "engagement_created":
        return "engagement", "info", f"New engagement: {client}", base
    if action in ("intake_submitted", "intake_saved"):
        if eng is not None and eng["stale"] and d.get("changed"):
            return (
                "assessment",
                "warning",
                f"{client}: intake changed after the assessment. Re-run it.",
                f"{base}/findings",
            )
        if action == "intake_submitted":
            return "engagement", "info", f"{client}: intake submitted", f"{base}/intake"
        return None
    if action in ("assessed", "assessed_with_claude"):
        how = " with Claude drafting" if action == "assessed_with_claude" else ""
        if d.get("unresolved"):
            return (
                "assessment",
                "critical",
                f"{client}: {d['unresolved']} citation(s) don't resolve. Delivery is blocked.",
                f"{base}/findings",
            )
        if d.get("documents_cleared"):
            return (
                "assessment",
                "warning",
                f"{client}: re-assessed{how}. Documents were cleared; regenerate the pack.",
                f"{base}/findings",
            )
        n = d.get("findings", 0)
        return (
            "assessment",
            "good",
            f"{client}: assessment complete{how} ({n} findings)",
            f"{base}/findings",
        )
    if action == "documents_generated":
        return "documents", "info", f"{client}: draft pack generated", f"{base}/documents"
    if action == "document_reviewed":
        left = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE engagement_id = ? AND reviewed_at IS NULL", (eid,)
        ).fetchone()[0]
        if not left:
            return "documents", "good", f"{client}: all documents reviewed. Ready to deliver.", base
        return (
            "documents",
            "info",
            f"{client}: document reviewed ({left} to go)",
            f"{base}/documents",
        )
    if action == "document_exported":
        return "documents", "info", f"{client}: document downloaded", f"{base}/documents"
    if action == "delivered":
        return "delivery", "good", f"{client}: delivered", base
    if action == "connector_added":
        name = _connector_name(d.get("connector", ""))
        return "connectors", "info", f"{client}: {name} connected", f"{base}/connectors"
    if action == "connector_removed":
        name = _connector_name(d.get("connector", ""))
        return "connectors", "info", f"{client}: {name} disconnected", f"{base}/connectors"
    if action == "connector_synced":
        name = _connector_name(d.get("connector", ""))
        if d.get("status") == "error":
            return "connectors", "serious", f"{client}: {name} check failed", f"{base}/connectors"
        failing = conn.execute(
            "SELECT COUNT(*) FROM evidence e JOIN connections c ON c.id = e.connection_id "
            "WHERE e.engagement_id = ? AND c.connector = ? AND e.status IN ('fail', 'warn')",
            (eid, d.get("connector")),
        ).fetchone()[0]
        if failing:
            return (
                "connectors",
                "warning",
                f"{client}: {name} found {failing} issue(s)",
                f"{base}/connectors",
            )
        return "connectors", "good", f"{client}: {name} checks passed", f"{base}/connectors"
    if action in ("dataflow_node_added", "dataflow_node_removed"):
        verb = "added to" if action == "dataflow_node_added" else "removed from"
        title = f"{client}: {d.get('name', 'a system')} {verb} the data-flow map"
        return "engagement", "info", title, f"{base}/dataflow"
    if action == "risk_updated":
        title = d.get("title", "a risk")
        if d.get("treatment") == "accept" and d.get("status") != "closed":
            return "assessment", "warning", f"{client}: risk accepted: {title}", f"{base}/risks"
        verb = "closed" if d.get("status") == "closed" else "updated"
        return "assessment", "info", f"{client}: risk {verb}: {title}", f"{base}/risks"
    if action == "github_app_created":
        return (
            "connectors",
            "info",
            f"GitHub App '{d.get('slug', '')}' set up",
            "/settings/github-app",
        )
    if action == "login_failed":
        return "security", "warning", f"Failed login attempt for '{actor}'", ""
    if action in ("user_created", "password_changed"):
        verb = "Account created" if action == "user_created" else "Password changed"
        return "security", "info", f"{verb}: {raw}", ""
    if action == "content_published":
        return "content", "good", f"Published: {d.get('slug', '')}", f"/content/{d.get('id', '')}"
    if action == "content_created":
        return "content", "info", f"New draft: {d.get('slug', '')}", f"/content/{d.get('id', '')}"
    return None


def from_audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    engagement_id: int | None,
    data: dict[str, Any],
    raw: str,
) -> None:
    rule = _rule(conn, actor, action, engagement_id, data, raw)
    if rule is None:
        return
    from grc_agent.web.db import now

    category, level, title, link = rule
    cur = conn.execute(
        "INSERT INTO notifications (at, actor, engagement_id, category, level, title, link) "
        "VALUES (?,?,?,?,?,?,?)",
        (now(), actor, engagement_id, category, level, title, link),
    )
    # Your own actions arrive already read; a failed login is someone else's action.
    if action != "login_failed":
        conn.execute(
            "INSERT OR IGNORE INTO notification_reads (username, notification_id) VALUES (?, ?)",
            (actor, cur.lastrowid),
        )


def unread_count(conn: sqlite3.Connection, user: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM notifications n WHERE NOT EXISTS (SELECT 1 FROM notification_reads r "
        "WHERE r.notification_id = n.id AND r.username = ?)",
        (user,),
    ).fetchone()[0]


def listing(
    conn: sqlite3.Connection,
    user: str,
    *,
    unread_only: bool = False,
    category: str = "",
    level: str = "",
    engagement_id: int | None = None,
    after_id: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where, args = ["n.id > ?"], [user, after_id]
    if unread_only:
        where.append("r.notification_id IS NULL")
    if category:
        where.append("n.category = ?")
        args.append(category)
    if level:
        where.append("n.level = ?")
        args.append(level)
    if engagement_id:
        where.append("n.engagement_id = ?")
        args.append(engagement_id)
    rows = conn.execute(
        "SELECT n.*, r.notification_id IS NOT NULL AS read FROM notifications n "
        "LEFT JOIN notification_reads r ON r.notification_id = n.id AND r.username = ? "
        f"WHERE {' AND '.join(where)} ORDER BY n.id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_read(conn: sqlite3.Connection, user: str, ids: list[int] | None = None) -> None:
    """Mark the given notifications read, or all of them."""
    if ids is None:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT n.id FROM notifications n WHERE NOT EXISTS (SELECT 1 FROM "
                "notification_reads r WHERE r.notification_id = n.id AND r.username = ?)",
                (user,),
            )
        ]
    conn.executemany(
        "INSERT OR IGNORE INTO notification_reads (username, notification_id) VALUES (?, ?)",
        [(user, i) for i in ids],
    )


def to_json(n: dict[str, Any]) -> str:
    return json.dumps({k: n[k] for k in ("id", "at", "level", "category", "title", "link")})
