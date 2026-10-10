"""The super admin console at /dashboard: invite people, run POCs, hand out access.

One page for the workspace owner to bring someone in by email with a role, see whose
proof of concept (a trial account) is about to end, extend or convert it, and give or
take away access to clients, without going engagement by engagement. Only a super
admin can open it (access.OWNER_PATHS).
"""

import re

from fastapi import FastAPI, HTTPException, Request

from grc_agent.web import access, auth_views, db, mailer

ROLE_CHOICES = {
    "client": "Client: their own company",
    "trial": "POC / trial: ends on a date",
    "partner": "Partner: runs DPDP for their clients",
    "admin": "Admin: every client and the team",
    "super_admin": "Super admin: owns the workspace",
}
POC_DAYS = (7, 14, 30, 60, 90)
ENDING_SOON_DAYS = 7


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


def poc_status(expires_at: str) -> str:
    left = access.days_left(expires_at)
    if not left:
        return "ended"
    return "ending" if left <= ENDING_SOON_DAYS else "active"


def overview(conn) -> dict:
    users = [
        dict(u)
        for u in conn.execute(
            "SELECT username, email, role, expires_at, created_at, "
            "password_hash LIKE '!%' AS no_password FROM users ORDER BY LOWER(username)"
        ).fetchall()
    ]
    engagements = conn.execute(
        "SELECT id, client, created_by FROM engagements ORDER BY LOWER(client)"
    ).fetchall()
    names = {e["id"]: e["client"] for e in engagements}
    granted: dict[str, list[dict]] = {}
    for a in conn.execute("SELECT engagement_id, username FROM engagement_access").fetchall():
        if a["engagement_id"] in names:
            granted.setdefault(a["username"].lower(), []).append(
                {"id": a["engagement_id"], "client": names[a["engagement_id"]]}
            )
    owned: dict[str, list[dict]] = {}
    for e in engagements:
        owned.setdefault(e["created_by"].lower(), []).append({"id": e["id"], "client": e["client"]})
    for u in users:
        key = u["username"].lower()
        u["granted"] = sorted(granted.get(key, []), key=lambda e: e["client"].lower())
        u["owned"] = owned.get(key, [])
        if u["role"] == "trial":
            u["days_left"] = access.days_left(u["expires_at"])
            u["status"] = poc_status(u["expires_at"])
    pocs = sorted(
        (u for u in users if u["role"] == "trial"), key=lambda u: u["expires_at"] or "9999"
    )
    counts = {r: sum(1 for u in users if u["role"] == r) for r in access.ROLES}
    return {
        "users": users,
        "pocs": pocs,
        "counts": counts,
        "poc_active": sum(1 for u in pocs if u["status"] != "ended"),
        "poc_ending": sum(1 for u in pocs if u["status"] == "ending"),
        "poc_ended": sum(1 for u in pocs if u["status"] == "ended"),
        "engagements": engagements,
    }


def register(app: FastAPI) -> None:
    from grc_agent.web.app import Conn, User, flash, form_with_csrf, redirect, render

    def page(request, conn, **extra):
        return render(
            request,
            "console.html",
            **overview(conn),
            role_choices=ROLE_CHOICES,
            poc_days=POC_DAYS,
            mail_on=mailer.configured(),
            **extra,
        )

    def account(conn, name: str):
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (name,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such account")
        return row

    def engagement_ids(conn, values) -> list[int]:
        values = values if isinstance(values, list) else [values] if values else []
        ids = [int(v) for v in values if str(v).isdigit()]
        return [
            i
            for i in ids
            if conn.execute("SELECT 1 FROM engagements WHERE id = ?", (i,)).fetchone()
        ]

    @app.get("/dashboard")
    def console(request: Request, user: User, conn: Conn):
        return page(request, conn)

    @app.post("/dashboard/invite")
    async def console_invite(request: Request, user: User, conn: Conn):
        """Create an account with a role and send (or show) its one-time invite link."""
        form = await form_with_csrf(request)
        email = str(form.get("email", "")).strip()
        role = str(form.get("role", "client"))
        if role not in access.ROLES:
            raise HTTPException(status_code=400, detail="Unknown role")
        if not email or "@" not in email:
            flash(request, "Enter the person's email address.", "error")
            return redirect("/dashboard")
        name = str(form.get("username", "")).strip() or username_from_email(conn, email)
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,40}", name):
            flash(request, "Use 2-40 letters, digits, dots, dashes or underscores.", "error")
            return redirect("/dashboard")
        if conn.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (name,)).fetchone():
            flash(request, f"{name} already has an account.", "error")
            return redirect("/dashboard")
        if problem := auth_views.email_problem(conn, email, name):
            flash(request, problem, "error")
            return redirect("/dashboard")
        days = (
            int(form.get("days", access.TRIAL_DAYS)) if str(form.get("days", "")).isdigit() else 0
        )
        days = days if days in POC_DAYS else access.TRIAL_DAYS
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role, email, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                name,
                auth_views.NO_PASSWORD,
                db.now(),
                role,
                email,
                access.trial_end(days) if role == "trial" else "",
            ),
        )
        db.audit(conn, user, "user_created", None, {"username": name, "role": role})
        for eid in engagement_ids(conn, form.get("engagements")):
            access.grant(conn, eid, name, user)
            db.audit(conn, user, "access_granted", eid, {"username": name})
        what = f"{name} ({ROLE_CHOICES[role].split(':')[0]})"
        if mailer.configured():
            try:
                auth_views.send_invite(conn, request, name, email, user)
            except mailer.MailError as exc:
                flash(request, f"Added {what}, but the email didn't send: {exc}", "error")
                return redirect("/dashboard")
            flash(request, f"Added {what} and emailed an invite to {email}.")
            return redirect("/dashboard")
        # No email service yet: show the link once so it can be shared another way.
        token = auth_views.issue_token(conn, name, "invite", user)
        db.audit(conn, user, "invite_link_shown", None, {"username": name})
        return page(
            request,
            conn,
            new_link=f"{auth_views.app_url(request)}/invite/{token}",
            new_name=name,
            new_email=email,
        )

    @app.post("/dashboard/people/{name}/resend")
    async def console_resend(name: str, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = account(conn, name)
        if auth_views.has_password(row):
            flash(request, f"{row['username']} has already set a password.", "error")
            return redirect("/dashboard")
        if mailer.configured() and row["email"]:
            try:
                auth_views.send_invite(conn, request, row["username"], row["email"], user)
            except mailer.MailError as exc:
                flash(request, f"The email didn't send: {exc}", "error")
                return redirect("/dashboard")
            flash(request, f"Emailed a new invite to {row['email']}.")
            return redirect("/dashboard")
        token = auth_views.issue_token(conn, row["username"], "invite", user)
        db.audit(conn, user, "invite_link_shown", None, {"username": row["username"]})
        return page(
            request,
            conn,
            new_link=f"{auth_views.app_url(request)}/invite/{token}",
            new_name=row["username"],
            new_email=row["email"],
        )

    @app.post("/dashboard/people/{name}/role")
    async def console_role(name: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        row = account(conn, name)
        role = str(form.get("role", ""))
        if role not in access.ROLES:
            raise HTTPException(status_code=400, detail="Unknown role")
        if row["role"] == "super_admin" and role != "super_admin":
            owners = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'super_admin'"
            ).fetchone()[0]
            if owners <= 1:
                flash(request, "Keep at least one super admin.", "error")
                return redirect("/dashboard")
        keep_end = row["role"] == "trial" and role == "trial"
        conn.execute(
            "UPDATE users SET role = ?, expires_at = ? WHERE username = ?",
            (
                role,
                row["expires_at"] if keep_end else access.trial_end() if role == "trial" else "",
                row["username"],
            ),
        )
        for key in [k for k in request.app.state.agents if k.lower() == name.lower()]:
            request.app.state.agents.pop(key, None)
        db.audit(conn, user, "role_changed", None, {"username": row["username"], "role": role})
        flash(request, f"{row['username']} is now {ROLE_CHOICES[role].split(':')[0]}.")
        return redirect("/dashboard#people")

    @app.post("/dashboard/poc/{name}/extend")
    async def console_extend(name: str, request: Request, user: User, conn: Conn):
        """Push a POC's end date out, from today or from its current end, whichever is later."""
        form = await form_with_csrf(request)
        row = account(conn, name)
        if row["role"] != "trial":
            raise HTTPException(status_code=400, detail="Not a POC account")
        days = int(form.get("days", 14)) if str(form.get("days", "")).isdigit() else 14
        days = days if days in POC_DAYS else 14
        left = access.days_left(row["expires_at"]) or 0
        end = access.trial_end(left + days)
        conn.execute("UPDATE users SET expires_at = ? WHERE username = ?", (end, row["username"]))
        db.audit(conn, user, "trial_extended", None, {"username": row["username"], "until": end})
        flash(request, f"{row['username']}'s POC now ends {end[:10]}.")
        return redirect("/dashboard#pocs")

    @app.post("/dashboard/poc/{name}/convert")
    async def console_convert(name: str, request: Request, user: User, conn: Conn):
        """The POC became a customer: a client account with no end date, same access."""
        await form_with_csrf(request)
        row = account(conn, name)
        conn.execute(
            "UPDATE users SET role = 'client', expires_at = '' WHERE username = ?",
            (row["username"],),
        )
        db.audit(conn, user, "role_changed", None, {"username": row["username"], "role": "client"})
        flash(
            request, f"{row['username']} is now a client. Their work and access stay as they were."
        )
        return redirect("/dashboard#pocs")

    @app.post("/dashboard/poc/{name}/end")
    async def console_end(name: str, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = account(conn, name)
        if row["role"] != "trial":
            raise HTTPException(status_code=400, detail="Not a POC account")
        end = db.now()
        conn.execute("UPDATE users SET expires_at = ? WHERE username = ?", (end, row["username"]))
        db.audit(conn, user, "trial_ended", None, {"username": row["username"]})
        flash(request, f"Ended {row['username']}'s POC. Their data stays; extend it to reopen.")
        return redirect("/dashboard#pocs")

    @app.post("/dashboard/people/{name}/grant")
    async def console_grant(name: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        row = account(conn, name)
        added = [
            eid
            for eid in engagement_ids(conn, form.get("engagements"))
            if access.grant(conn, eid, row["username"], user)
        ]
        for eid in added:
            db.audit(conn, user, "access_granted", eid, {"username": row["username"]})
        flash(
            request,
            f"{row['username']} can now open {len(added)} more client(s)."
            if added
            else "Pick a client they don't have yet.",
            "info" if added else "error",
        )
        return redirect("/dashboard#people")

    @app.post("/dashboard/people/{name}/revoke/{eid}")
    async def console_revoke(name: str, eid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = account(conn, name)
        access.revoke(conn, eid, row["username"])
        db.audit(conn, user, "access_removed", eid, {"username": row["username"]})
        flash(request, f"Removed {row['username']}'s access.")
        return redirect("/dashboard#people")
