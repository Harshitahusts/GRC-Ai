"""The super admin dashboard at /dashboard: organisations, POCs, people and access.

A hidden page for the workspace owner (access.OWNER_PATHS; anyone else gets "not
found"). From here the owner sets up a customer organisation (a company or a partner),
decides whether it is a POC and for how many days, invites its people with their team
role (Admin / Manager / Viewer), and gives or takes away access to engagements. The
platform roles (super admin, GRC Flow admin) are only ever set here.
"""

from fastapi import FastAPI, HTTPException, Request

from grc_agent.web import access, db, invites, mailer

ENDING_SOON_DAYS = 7
MAX_POC_DAYS = 365


def poc_status(poc_until: str) -> str:
    if not poc_until:
        return "permanent"
    left = access.days_left(poc_until)
    if not left:
        return "ended"
    return "ending" if left <= ENDING_SOON_DAYS else "active"


def days_from(value) -> int | None:
    """A POC length typed by the owner: a whole number of days, 1 to MAX_POC_DAYS."""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    days = int(text)
    return days if 1 <= days <= MAX_POC_DAYS else None


def overview(conn) -> dict:
    engagements = conn.execute(
        "SELECT id, client, created_by, org_id FROM engagements ORDER BY LOWER(client)"
    ).fetchall()
    names = {e["id"]: e["client"] for e in engagements}
    granted: dict[str, list[dict]] = {}
    for a in conn.execute("SELECT engagement_id, username FROM engagement_access").fetchall():
        if a["engagement_id"] in names:
            granted.setdefault(a["username"].lower(), []).append(
                {"id": a["engagement_id"], "client": names[a["engagement_id"]]}
            )
    users = [
        dict(u)
        for u in conn.execute(
            "SELECT username, email, role, org_id, team_role, created_at, "
            "password_hash LIKE '!%' AS invited FROM users ORDER BY LOWER(username)"
        ).fetchall()
    ]
    for u in users:
        u["granted"] = sorted(granted.get(u["username"].lower(), []), key=lambda e: e["client"])
    orgs = []
    for o in conn.execute("SELECT * FROM orgs ORDER BY LOWER(name)").fetchall():
        org = dict(o)
        org["members"] = [u for u in users if u["org_id"] == o["id"]]
        org["engagements"] = [e for e in engagements if e["org_id"] == o["id"]]
        org["status"] = poc_status(o["poc_until"])
        org["days_left"] = access.days_left(o["poc_until"])
        orgs.append(org)
    org_names = {o["id"]: o["name"] for o in orgs}
    for u in users:
        u["org_name"] = org_names.get(u["org_id"], "")
    pocs = [o for o in orgs if o["status"] != "permanent"]
    # POCs first, ending soonest; then the rest by name.
    orgs.sort(key=lambda o: (o["status"] == "permanent", o["poc_until"] or "", o["name"].lower()))
    return {
        "orgs": orgs,
        "users": users,
        "staff": [u for u in users if u["role"] in access.STAFF],
        "guests": [u for u in users if u["role"] not in access.STAFF and u["org_id"] is None],
        "engagements": engagements,
        "companies": sum(1 for o in orgs if o["kind"] == "client"),
        "partners": sum(1 for o in orgs if o["kind"] == "partner"),
        "poc_active": sum(1 for o in pocs if o["status"] in ("active", "ending")),
        "poc_ending": sum(1 for o in pocs if o["status"] == "ending"),
        "poc_ended": sum(1 for o in pocs if o["status"] == "ended"),
    }


def register(app: FastAPI) -> None:
    from grc_agent.web.app import Conn, User, flash, form_with_csrf, redirect, render

    def page(request, conn, **extra):
        return render(
            request,
            "console.html",
            **overview(conn),
            team_roles=access.TEAM_ROLES,
            platform_roles=access.PLATFORM_ROLES,
            org_kinds=access.ORG_KINDS,
            max_poc_days=MAX_POC_DAYS,
            mail_on=mailer.configured(),
            **extra,
        )

    def account_row(conn, name: str):
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (name,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such account")
        return row

    def org_row(conn, oid: int):
        row = conn.execute("SELECT * FROM orgs WHERE id = ?", (oid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such organisation")
        return row

    def engagement_ids(conn, values) -> list[int]:
        values = values if isinstance(values, list) else [values] if values else []
        ids = [int(v) for v in values if str(v).isdigit()]
        return [
            i
            for i in ids
            if conn.execute("SELECT 1 FROM engagements WHERE id = ?", (i,)).fetchone()
        ]

    def drop_agent(request, name: str) -> None:
        # The analyst's tools depend on the person's access, so it starts afresh.
        for key in [k for k in request.app.state.agents if k.lower() == name.lower()]:
            request.app.state.agents.pop(key, None)

    @app.get("/dashboard")
    def console(request: Request, user: User, conn: Conn):
        return page(request, conn)

    @app.post("/dashboard/accounts")
    async def console_add(request: Request, user: User, conn: Conn):
        """Add a person: into a new organisation, an existing one, or GRC Flow's staff."""
        form = await form_with_csrf(request)
        target = str(form.get("org", "new"))
        email = str(form.get("email", ""))
        team_role = str(form.get("team_role", "admin"))
        if team_role not in access.TEAM_ROLES:
            raise HTTPException(status_code=400, detail="Unknown team role")
        role, org_id, created = "user", None, ""
        if target == "staff":
            role, team_role = "admin", "admin"
        elif target == "new":
            name = str(form.get("org_name", "")).strip()[:120]
            kind = str(form.get("kind", "client"))
            if not name or kind not in access.ORG_KINDS:
                flash(request, "Give the organisation a name and say what it is.", "error")
                return redirect("/dashboard#add")
            days = None
            if form.get("poc"):
                days = days_from(form.get("poc_days"))
                if days is None:
                    flash(request, f"Give the POC 1 to {MAX_POC_DAYS} days.", "error")
                    return redirect("/dashboard#add")
            org_id = access.new_org(conn, name, kind, days, user)
            db.audit(conn, user, "org_created", None, {"org": name, "kind": kind, "poc_days": days})
            created = name
        elif target.isdigit():
            org_id = org_row(conn, int(target))["id"]
        else:
            raise HTTPException(status_code=400, detail="Pick an organisation")
        result = invites.invite(
            conn,
            request,
            user,
            email,
            username=str(form.get("username", "")),
            role=role,
            org_id=org_id,
            team_role=team_role,
        )
        if not result.ok:
            if created:  # don't leave an empty organisation behind
                conn.execute("DELETE FROM orgs WHERE id = ?", (org_id,))
            flash(request, result.message, "error")
            return redirect("/dashboard#add")
        for eid in engagement_ids(conn, form.get("engagements")):
            access.grant(conn, eid, result.username, user)
            db.audit(conn, user, "access_granted", eid, {"username": result.username})
        invites.remember_link(request, result, email)
        flash(request, (f"Created {created}. " if created else "") + result.message)
        return redirect("/dashboard")

    # ---- organisations and POCs

    @app.post("/dashboard/orgs/{oid}/poc")
    async def console_poc(oid: int, request: Request, user: User, conn: Conn):
        """Set or extend a POC by any number of days: from its current end while it's
        running, else from today (which also turns a permanent organisation into a POC)."""
        form = await form_with_csrf(request)
        org = org_row(conn, oid)
        days = days_from(form.get("days"))
        if days is None:
            flash(request, f"Give 1 to {MAX_POC_DAYS} days.", "error")
            return redirect("/dashboard#orgs")
        left = access.days_left(org["poc_until"]) or 0
        end = access.end_after(left + days)
        conn.execute("UPDATE orgs SET poc_until = ? WHERE id = ?", (end, oid))
        db.audit(conn, user, "poc_extended", None, {"org": org["name"], "until": end})
        flash(request, f"{org['name']}'s POC now ends {end[:10]}.")
        return redirect("/dashboard#orgs")

    @app.post("/dashboard/orgs/{oid}/convert")
    async def console_convert(oid: int, request: Request, user: User, conn: Conn):
        """The POC became a customer: no end date; its people and work stay as they are."""
        await form_with_csrf(request)
        org = org_row(conn, oid)
        conn.execute("UPDATE orgs SET poc_until = '' WHERE id = ?", (oid,))
        db.audit(conn, user, "poc_converted", None, {"org": org["name"]})
        flash(request, f"{org['name']} is now a customer, with no end date.")
        return redirect("/dashboard#orgs")

    @app.post("/dashboard/orgs/{oid}/end")
    async def console_end(oid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        org = org_row(conn, oid)
        conn.execute("UPDATE orgs SET poc_until = ? WHERE id = ?", (db.now(), oid))
        db.audit(conn, user, "poc_ended", None, {"org": org["name"]})
        flash(request, f"Ended {org['name']}'s access. Their data stays; extend to reopen it.")
        return redirect("/dashboard#orgs")

    # ---- people

    @app.post("/dashboard/people/{name}/team-role")
    async def console_team_role(name: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        row = account_row(conn, name)
        team_role = str(form.get("team_role", ""))
        if team_role not in access.TEAM_ROLES:
            raise HTTPException(status_code=400, detail="Unknown team role")
        conn.execute(
            "UPDATE users SET team_role = ? WHERE username = ?", (team_role, row["username"])
        )
        drop_agent(request, row["username"])
        db.audit(
            conn, user, "team_role_changed", None, {"username": row["username"], "to": team_role}
        )
        flash(request, f"{row['username']} is now {team_role}.")
        return redirect("/dashboard#people")

    @app.post("/dashboard/people/{name}/role")
    async def console_platform_role(name: str, request: Request, user: User, conn: Conn):
        """Platform roles: customer, GRC Flow admin, super admin. Only ever set here."""
        form = await form_with_csrf(request)
        row = account_row(conn, name)
        role = str(form.get("role", ""))
        if role not in access.PLATFORM_ROLES:
            raise HTTPException(status_code=400, detail="Unknown role")
        if row["role"] == "super_admin" and role != "super_admin":
            owners = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'super_admin'"
            ).fetchone()[0]
            if owners <= 1:
                flash(request, "Keep at least one super admin.", "error")
                return redirect("/dashboard#people")
        conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, row["username"]))
        drop_agent(request, row["username"])
        db.audit(conn, user, "role_changed", None, {"username": row["username"], "role": role})
        flash(request, f"{row['username']}: {access.PLATFORM_ROLES[role].split(':')[0]}.")
        return redirect("/dashboard#people")

    @app.post("/dashboard/people/{name}/resend")
    async def console_resend(name: str, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = account_row(conn, name)
        if not str(row["password_hash"]).startswith("!"):
            flash(request, f"{row['username']} has already set a password.", "error")
            return redirect("/dashboard#people")
        result = invites.send(conn, request, user, row["username"], row["email"])
        invites.remember_link(request, result, row["email"])
        flash(request, result.message, "info" if result.ok else "error")
        return redirect("/dashboard")

    @app.post("/dashboard/people/{name}/remove")
    async def console_remove(name: str, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = account_row(conn, name)
        if row["username"].lower() == user.lower():
            flash(request, "You can't remove yourself.", "error")
            return redirect("/dashboard#people")
        invites.remove_account(conn, row["username"], user)
        drop_agent(request, row["username"])
        flash(request, f"Removed {row['username']}.")
        return redirect("/dashboard#people")

    @app.post("/dashboard/people/{name}/grant")
    async def console_grant(name: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        row = account_row(conn, name)
        added = [
            eid
            for eid in engagement_ids(conn, form.get("engagements"))
            if access.grant(conn, eid, row["username"], user)
        ]
        for eid in added:
            db.audit(conn, user, "access_granted", eid, {"username": row["username"]})
        flash(
            request,
            f"{row['username']} can now open {len(added)} more engagement(s)."
            if added
            else "Pick an engagement they don't have yet.",
            "info" if added else "error",
        )
        return redirect("/dashboard#people")

    @app.post("/dashboard/people/{name}/revoke/{eid}")
    async def console_revoke(name: str, eid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = account_row(conn, name)
        access.revoke(conn, eid, row["username"])
        db.audit(conn, user, "access_removed", eid, {"username": row["username"]})
        flash(request, f"Removed {row['username']}'s access.")
        return redirect("/dashboard#people")
