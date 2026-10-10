"""Bringing people in: one way to create an account and get its invite to the person.

Used by an organisation's Team & roles page, an engagement's People with access box and
the super admin dashboard. Nobody types a password for someone else: the account starts
without one, and the person sets it from a one-time link. With email set up (Resend) the
link is emailed; without it, it is shown once to the inviter to pass on.
"""

import re
from dataclasses import dataclass

from fastapi import Request

from grc_agent.web import auth_views, db, mailer

USERNAME = re.compile(r"[A-Za-z0-9._-]{2,40}")


@dataclass
class Invited:
    ok: bool
    message: str
    username: str = ""
    link: str = ""  # only when email isn't set up: the one-time link to pass on


def username_from_email(conn, email: str) -> str:
    """A free username made from the part of the email before the @."""
    base = re.sub(r"[^A-Za-z0-9._-]", "", email.split("@", 1)[0])[:34] or "user"
    if len(base) < 2:
        base = f"{base}-user"
    name, n = base, 1
    while conn.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (name,)).fetchone():
        n += 1
        name = f"{base}{n}"
    return name


def invite(
    conn,
    request: Request,
    by: str,
    email: str,
    *,
    username: str = "",
    role: str = "user",
    org_id: int | None = None,
    team_role: str = "viewer",
) -> Invited:
    email = email.strip()
    if not email or "@" not in email:
        return Invited(False, "Enter the person's email address.")
    name = username.strip() or username_from_email(conn, email)
    if not USERNAME.fullmatch(name):
        return Invited(False, "Use 2-40 letters, digits, dots, dashes or underscores.")
    if conn.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (name,)).fetchone():
        return Invited(False, f"{name} already has an account.")
    if problem := auth_views.email_problem(conn, email, name):
        return Invited(False, problem)
    conn.execute(
        "INSERT INTO users (username, password_hash, created_at, role, email, org_id, team_role) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, auth_views.NO_PASSWORD, db.now(), role, email, org_id, team_role),
    )
    db.audit(
        conn, by, "user_created", None, {"username": name, "role": role, "team_role": team_role}
    )
    return send(conn, request, by, name, email)


def send(conn, request: Request, by: str, name: str, email: str) -> Invited:
    """Email a fresh invite link, or return it to show once when email isn't set up."""
    if mailer.configured() and email:
        try:
            auth_views.send_invite(conn, request, name, email, by)
        except mailer.MailError as exc:
            return Invited(True, f"Added {name}, but the email didn't send: {exc}", name)
        return Invited(True, f"Invited {name}: the email is on its way to {email}.", name)
    token = auth_views.issue_token(conn, name, "invite", by)
    db.audit(conn, by, "invite_link_shown", None, {"username": name})
    link = f"{auth_views.app_url(request)}/invite/{token}"
    return Invited(True, f"Added {name}. Send them the invite link below.", name, link)


def remember_link(request: Request, invited: Invited, email: str) -> None:
    """Keep a one-time link for the next page view only, so it can be shown and copied."""
    if invited.link:
        request.session["invite_link"] = {
            "name": invited.username,
            "email": email,
            "link": invited.link,
        }


def remove_account(conn, name: str, by: str) -> None:
    """Take someone out completely: their sign-ins, access, open links and API keys go too."""
    conn.execute("DELETE FROM login_identities WHERE LOWER(username) = LOWER(?)", (name,))
    conn.execute("DELETE FROM engagement_access WHERE LOWER(username) = LOWER(?)", (name,))
    conn.execute(
        "UPDATE auth_tokens SET used_at = ? WHERE LOWER(username) = LOWER(?) AND used_at IS NULL",
        (db.now(), name),
    )
    conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE LOWER(username) = LOWER(?) "
        "AND revoked_at IS NULL",
        (db.now(), name),
    )
    conn.execute("DELETE FROM users WHERE LOWER(username) = LOWER(?)", (name,))
    db.audit(conn, by, "user_removed", None, {"username": name})
