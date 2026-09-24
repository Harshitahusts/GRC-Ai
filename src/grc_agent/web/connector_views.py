"""Connector pages, evidence, notifications and intake/evidence contradiction checks."""

# No "from __future__ import annotations" here: the routes are defined inside register()
# with dependency types imported there, and FastAPI must be able to resolve them.

import hashlib
import hmac
import json
import logging
import os
import secrets as secrets_lib
import sqlite3
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from grc_agent.connectors import ConnectorError, by_category, cloud, github_app
from grc_agent.connectors.secrets import mask
from grc_agent.kpis.citations import normalize_citation
from grc_agent.web import db

log = logging.getLogger(__name__)

PROJECT_URL = "https://github.com/Harshitahusts/GRC-Ai"


# ---- evidence helpers (used by other pages too)


def evidence_rows(conn: sqlite3.Connection, eid: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT e.*, c.connector FROM evidence e JOIN connections c ON c.id = e.connection_id "
        "WHERE e.engagement_id = ? ORDER BY c.id, e.id",
        (eid,),
    )
    return [
        {
            **dict(r),
            "provisions": json.loads(r["provisions_json"]),
            "data": json.loads(r["data_json"]),
        }
        for r in rows
    ]


def evidence_for_provision(evidence: list[dict], provision: str) -> list[dict]:
    """Evidence linked to a provision, or to a sub-provision / parent of it."""
    key = normalize_citation(provision)
    if key is None:
        return []
    out = []
    for e in evidence:
        for p in e["provisions"]:
            k = normalize_citation(p)
            if k and (k == key or k.startswith(key + "(") or key.startswith(k + "(")):
                out.append(e)
                break
    return out


def conflicts(
    conn: sqlite3.Connection, eid: int, answers: dict[str, str], connectors: dict
) -> list[str]:
    """Where connector evidence contradicts what the client said at intake."""
    out = []
    foreign = answers.get("CTX-FOREIGN")
    for e in evidence_rows(conn, eid):
        outside = e["data"].get("outside_india") or []
        if e["check_key"] != "data_location" or not outside:
            continue
        name = connectors[e["connector"]].name if e["connector"] in connectors else e["connector"]
        regions = ", ".join(outside)
        if foreign == "no":
            out.append(
                f"The client answered No to using services outside India, but {name} shows data "
                f"stored in {regions}. Check with the client and correct the intake answer."
            )
        elif foreign in (None, "not_sure"):
            out.append(
                f"{name} shows data stored outside India ({regions}), but the intake question on "
                "services outside India is unanswered or 'not sure'. Update the answer."
            )
    return out


# ---- notifications


def queue_notification(
    request: Request, background: BackgroundTasks, conn: sqlite3.Connection, eid: int, text: str
) -> None:
    """Send `text` to every chat connector on the engagement, after the response.

    Messages carry workflow status only, never client personal data.
    """
    connectors = request.app.state.connectors
    box = request.app.state.secret_box
    targets = []
    for row in conn.execute("SELECT * FROM connections WHERE engagement_id = ?", (eid,)):
        c = connectors.get(row["connector"])
        if c is None or c.kind != "notify" or c.status != "available":
            continue
        try:
            targets.append(
                (row["id"], c, json.loads(row["config_json"]), box.open(row["secrets_enc"]))
            )
        except ValueError:
            continue
    if targets:
        # Commit the request's changes first: the background task writes delivery
        # results on its own connection and must not wait on this one's lock.
        # Callers make this the last database step of the request.
        conn.commit()
        background.add_task(_send_all, request.app.state.db_path, targets, f"[GRC agent] {text}")


def _send_all(db_path, targets, text: str) -> None:
    with db.connect(db_path) as conn:
        for cid, c, config, secrets in targets:
            try:
                c.send(config, secrets, text)
                conn.execute(
                    "UPDATE connections SET status = 'ok', message = ? WHERE id = ?",
                    (f"Last message sent {db.now()[:16].replace('T', ' ')} UTC", cid),
                )
            except ConnectorError as exc:
                conn.execute(
                    "UPDATE connections SET status = 'error', message = ? WHERE id = ?",
                    (str(exc), cid),
                )


# ---- authorisation helpers


def external_id(app: FastAPI, eid: int) -> str:
    """The AWS external ID for an engagement: generated by us (never by the client),
    unguessable, and stable, so the client's role only works for this engagement."""
    digest = hmac.new(
        app.state.secret_key.encode(), f"aws-external-id:{eid}".encode(), hashlib.sha256
    )
    return "grc-" + digest.hexdigest()[:32]


def _signer(app: FastAPI, purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.state.secret_key, salt=purpose)


def github_config(app: FastAPI):
    return github_app.load_config(app.state.data_dir, app.state.secret_box)


# ---- routes


def register(app: FastAPI) -> None:
    from grc_agent.web.app import (
        Conn,
        User,
        _summary,
        flash,
        form_with_csrf,
        get_engagement,
        redirect,
        render,
    )

    def _connection(conn: sqlite3.Connection, eid: int, cid: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = ? AND engagement_id = ?", (cid, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        return row

    def _connector(request: Request, connector_id: str):
        c = request.app.state.connectors.get(connector_id)
        if c is None or c.status != "available":
            raise HTTPException(status_code=404, detail="Unknown or unavailable connector")
        return c

    async def _sync(
        request: Request, conn: sqlite3.Connection, row: sqlite3.Row, user: str
    ) -> None:
        """Collect evidence (or send a test message) and record the outcome."""
        c = _connector(request, row["connector"])
        config = json.loads(row["config_json"])
        try:
            secrets = request.app.state.secret_box.open(row["secrets_enc"])
            if c.kind == "notify":
                await run_in_threadpool(
                    c.send, config, secrets, "[GRC agent] Test message: this channel is connected."
                )
                message, checks = "Test message sent.", None
            elif c.flow == "github_app":
                app_config = github_config(request.app)
                if app_config is None:
                    raise ConnectorError("The firm's GitHub App isn't set up on this computer.")
                checks = await run_in_threadpool(
                    github_app.collect, app_config, config["installation_id"]
                )
                message = f"{len(checks)} checks collected from {config.get('account')}."
            else:
                checks = await run_in_threadpool(c.collect, config, secrets)
                message = f"{len(checks)} checks collected."
        except (ConnectorError, ValueError) as exc:
            status, message = "error", str(exc)
        except Exception:  # an unexpected API response shouldn't crash the page
            log.exception("Connector %s failed", c.id)
            status, message = "error", f"{c.name} returned something unexpected. Try again later."
        else:
            status = "ok"
            if checks is not None:
                conn.execute("DELETE FROM evidence WHERE connection_id = ?", (row["id"],))
                conn.executemany(
                    "INSERT INTO evidence (connection_id, engagement_id, check_key, title, status, "
                    "detail, provisions_json, data_json, collected_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            row["id"],
                            row["engagement_id"],
                            ch.key,
                            ch.title,
                            ch.status,
                            ch.detail,
                            json.dumps(ch.provisions),
                            json.dumps(ch.data),
                            db.now(),
                        )
                        for ch in checks
                    ],
                )
        conn.execute(
            "UPDATE connections SET status = ?, message = ?, last_synced_at = ? WHERE id = ?",
            (status, message, db.now(), row["id"]),
        )
        db.audit(
            conn,
            user,
            "connector_synced",
            row["engagement_id"],
            {"connector": c.id, "status": status},
        )
        flash(request, f"{c.name}: {message}", "error" if status == "error" else "info")

    async def _save_connection(
        request: Request,
        conn: sqlite3.Connection,
        eid: int,
        c,
        config: dict,
        secrets: dict,
        message: str,
        user: str,
    ) -> None:
        """Store a verified connection (credentials encrypted), then collect evidence."""
        cur = conn.execute(
            "INSERT INTO connections (engagement_id, connector, config_json, secrets_enc, "
            "secret_hints_json, message, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                eid,
                c.id,
                json.dumps(config),
                request.app.state.secret_box.seal(secrets),
                json.dumps({k: mask(v) for k, v in secrets.items()}),
                message,
                user,
                db.now(),
            ),
        )
        db.audit(conn, user, "connector_added", eid, {"connector": c.id})
        flash(request, message)
        if c.kind == "evidence":
            await _sync(request, conn, _connection(conn, eid, cur.lastrowid), user)

    async def _save_github(request, conn, eid: int, inst: dict, user: str, verb: str) -> None:
        """Add the installation to the engagement, or re-check it if it's already there
        (GitHub sends the client back here when they change which repositories to share)."""
        info = github_app.describe(inst)
        for row in conn.execute(
            "SELECT * FROM connections WHERE engagement_id = ? AND connector = 'github'", (eid,)
        ).fetchall():
            if json.loads(row["config_json"]).get("installation_id") == info["installation_id"]:
                conn.execute(
                    "UPDATE connections SET config_json = ? WHERE id = ?",
                    (json.dumps(info), row["id"]),
                )
                await _sync(request, conn, _connection(conn, eid, row["id"]), user)
                return
        await _save_connection(
            request,
            conn,
            eid,
            _connector(request, "github"),
            info,
            {},
            f"{verb} {info['account']}.",
            user,
        )

    @app.get("/connectors")
    def connectors_page(request: Request, user: User, conn: Conn):
        rows = conn.execute(
            "SELECT c.*, e.client FROM connections c JOIN engagements e ON e.id = c.engagement_id "
            "ORDER BY c.id DESC"
        ).fetchall()
        return render(request, "connectors.html", categories=by_category(), connections=rows)

    @app.get("/connectors/{connector_id}")
    def connector_detail(connector_id: str, request: Request, user: User, conn: Conn):
        c = request.app.state.connectors.get(connector_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Unknown connector")
        engagements = conn.execute(
            "SELECT id, client, sector FROM engagements WHERE mode = 'agent' ORDER BY id DESC"
        ).fetchall()
        connections = conn.execute(
            "SELECT c.*, e.client FROM connections c JOIN engagements e ON e.id = c.engagement_id "
            "WHERE c.connector = ? ORDER BY c.id DESC",
            (c.id,),
        ).fetchall()
        return render(
            request, "connector_detail.html", c=c, engagements=engagements, connections=connections
        )

    @app.get("/connectors/{connector_id}/connect")
    def connector_connect(
        connector_id: str, request: Request, user: User, conn: Conn, engagement: int = 0
    ):
        """From the catalog: pick an engagement, then go to its setup form for this connector."""
        c = _connector(request, connector_id)
        eng = get_engagement(conn, engagement)
        if eng["mode"] != "agent":
            raise HTTPException(
                status_code=400, detail="Connectors are for agent-assisted engagements."
            )
        return redirect(f"/engagements/{eng['id']}/connectors/new?type={c.id}")

    @app.get("/engagements/{eid}/connectors")
    def engagement_connectors(eid: int, request: Request, user: User, conn: Conn):
        from grc_agent.web.app import answers_of

        eng = get_engagement(conn, eid)
        connectors = request.app.state.connectors
        rows = conn.execute(
            "SELECT * FROM connections WHERE engagement_id = ? ORDER BY id", (eid,)
        ).fetchall()
        evidence = evidence_rows(conn, eid)
        connections = [
            {
                **dict(r),
                "connector_obj": connectors.get(r["connector"]),
                "config": json.loads(r["config_json"]),
                "hints": json.loads(r["secret_hints_json"]),
                "evidence": [e for e in evidence if e["connection_id"] == r["id"]],
            }
            for r in rows
        ]
        return render(
            request,
            "engagement_connectors.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="connectors",
            connections=connections,
            categories=by_category(),
            conflicts=conflicts(conn, eid, answers_of(conn, eid), connectors),
        )

    @app.get("/engagements/{eid}/connectors/new")
    def connector_new(eid: int, request: Request, user: User, conn: Conn, type: str = ""):
        eng = get_engagement(conn, eid)
        c = _connector(request, type)
        ctx: dict[str, Any] = {"eng": eng, "s": _summary(conn, eng), "tab": "connectors", "c": c}
        if c.flow == "github_app":
            config = github_config(request.app)
            state = _signer(request.app, "github-install").dumps({"eid": eid, "user": user})
            ctx.update(
                app_config=config,
                install_url=github_app.install_url(config, state) if config else None,
            )
            return render(request, "connector_github.html", **ctx)
        if c.flow == "aws_role":
            try:
                ctx["firm_account"] = cloud.firm_account_id()
            except ConnectorError as exc:
                ctx["firm_error"] = str(exc)
            ctx["external_id"] = external_id(request.app, eid)
            return render(request, "connector_aws.html", **ctx)
        return render(request, "connector_new.html", **ctx)

    @app.post("/engagements/{eid}/connectors")
    async def connector_create(eid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        c = _connector(request, str(form.get("connector", "")))
        if c.flow == "github_app":
            raise HTTPException(
                status_code=400, detail="GitHub is connected by authorising the app."
            )
        config, secrets, missing = {}, {}, []
        for f in c.fields:
            raw = str(form.get(f.name, ""))
            value = raw.strip() if not f.multiline else raw.strip("\n")
            if not value.strip():
                if f.required:
                    missing.append(f.label)
                continue
            (secrets if f.secret else config)[f.name] = value[:20_000]
        if c.flow == "aws_role":
            config["external_id"] = external_id(request.app, eid)
        back = f"/engagements/{eid}/connectors/new?type={c.id}"
        if missing:
            flash(request, "Fill in: " + ", ".join(missing) + ".", "error")
            return redirect(back)
        try:
            if c.kind == "notify":
                await run_in_threadpool(
                    c.send,
                    config,
                    secrets,
                    f"[GRC agent] Connected for engagement {eng['client']}. "
                    "Updates will be posted here.",
                )
                message = "Connected. A test message was sent."
            else:
                message = await run_in_threadpool(c.test, config, secrets)
        except ConnectorError as exc:
            flash(request, f"Couldn't connect to {c.name}: {exc}", "error")
            return redirect(back)
        except Exception:
            log.exception("Connector %s test failed", c.id)
            flash(request, f"Couldn't connect to {c.name}: unexpected response.", "error")
            return redirect(back)

        await _save_connection(request, conn, eid, c, config, secrets, message, user)
        return redirect(f"/engagements/{eid}/connectors")

    @app.post("/engagements/{eid}/connectors/{cid}/sync")
    async def connector_sync(eid: int, cid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        get_engagement(conn, eid)
        await _sync(request, conn, _connection(conn, eid, cid), user)
        return redirect(f"/engagements/{eid}/connectors")

    @app.post("/engagements/{eid}/connectors/{cid}/delete")
    async def connector_delete(eid: int, cid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = _connection(conn, eid, cid)
        conn.execute("DELETE FROM evidence WHERE connection_id = ?", (cid,))
        conn.execute("DELETE FROM connections WHERE id = ?", (cid,))
        db.audit(conn, user, "connector_removed", eid, {"connector": row["connector"]})
        flash(request, "Connection removed and its stored credentials deleted.")
        return redirect(f"/engagements/{eid}/connectors")

    # ---- GitHub: one-time app setup (manifest flow), then per-client installs

    @app.get("/settings/github-app")
    def github_app_settings(request: Request, user: User):
        base = str(request.base_url).rstrip("/")
        # GitHub needs a public homepage and webhook address, even though webhooks are off.
        # A local app (http://localhost) uses the project's page instead.
        homepage = (os.getenv("GRC_PUBLIC_URL") or "").rstrip("/")
        if not homepage:
            homepage = base if base.startswith("https://") else PROJECT_URL
        manifest = github_app.manifest("", base, homepage)
        state = _signer(request.app, "github-manifest").dumps({"user": user})
        return render(
            request,
            "settings_github_app.html",
            app_config=github_config(request.app),
            manifest=manifest,
            state=state,
            default_name=f"GRC agent {secrets_lib.token_hex(3)}",
        )

    @app.get("/settings/github-app/callback")
    async def github_app_callback(
        request: Request, user: User, conn: Conn, code: str = "", state: str = ""
    ):
        try:
            data = _signer(request.app, "github-manifest").loads(state, max_age=3600)
            if data.get("user") != user:
                raise BadSignature("different user")
        except BadSignature:
            raise HTTPException(
                status_code=400, detail="This setup link expired. Start again."
            ) from None
        try:
            config = await run_in_threadpool(github_app.convert_manifest, code)
        except ConnectorError as exc:
            flash(request, f"GitHub App setup failed: {exc}", "error")
            return redirect("/settings/github-app")
        github_app.save_config(request.app.state.data_dir, request.app.state.secret_box, config)
        db.audit(conn, user, "github_app_created", detail={"slug": config.slug})
        flash(
            request,
            f"GitHub App '{config.slug}' created. Clients can now authorise read-only access.",
        )
        return redirect("/settings/github-app")

    @app.get("/connectors/github/setup")
    async def github_setup(
        request: Request,
        conn: Conn,
        installation_id: str = "",
        setup_action: str = "",
        state: str = "",
    ):
        """Where GitHub sends the browser after the app is installed."""
        user = request.session.get("user")
        eid = None
        if state:
            try:
                data = _signer(request.app, "github-install").loads(state, max_age=7 * 24 * 3600)
                eid = int(data["eid"]) if data.get("user") == user else None
            except (BadSignature, KeyError, ValueError):
                eid = None
        if not user or eid is None:
            # Usually the client, installing from their own computer.
            return render(request, "github_installed.html", setup_action=setup_action)
        config = github_config(request.app)
        if config is None:
            raise HTTPException(status_code=400, detail="The firm's GitHub App isn't set up.")
        _connector(request, "github")  # 404 if GitHub is switched off
        get_engagement(conn, eid)
        if setup_action == "request" or not installation_id:
            flash(
                request,
                "GitHub sent an install request to the organisation's owners. Once "
                "an owner approves it, use 'Find their installation' below.",
                "warn",
            )
            return redirect(f"/engagements/{eid}/connectors/new?type=github")
        try:
            inst = await run_in_threadpool(github_app.get_installation, config, installation_id)
        except ConnectorError as exc:
            flash(request, f"GitHub: {exc}", "error")
            return redirect(f"/engagements/{eid}/connectors/new?type=github")
        await _save_github(request, conn, eid, inst, user, "Authorised by GitHub account")
        return redirect(f"/engagements/{eid}/connectors")

    @app.post("/engagements/{eid}/connectors/github/find")
    async def github_find(eid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        config = github_config(request.app)
        account = str(form.get("account", "")).strip()
        back = f"/engagements/{eid}/connectors/new?type=github"
        if config is None or not account:
            flash(request, "Enter the client's GitHub organisation or account name.", "error")
            return redirect(back)
        try:
            inst = await run_in_threadpool(github_app.find_installation, config, account)
        except ConnectorError as exc:
            flash(request, f"GitHub: {exc}", "error")
            return redirect(back)
        await _save_github(request, conn, eid, inst, user, "Found the app installed on")
        return redirect(f"/engagements/{eid}/connectors")

    # ---- AWS: the CloudFormation template the client runs

    @app.get("/engagements/{eid}/connectors/aws/template.yaml")
    def aws_template(eid: int, request: Request, user: User, conn: Conn):
        eng = get_engagement(conn, eid)
        try:
            account = cloud.firm_account_id()
        except ConnectorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        body = cloud.cloudformation_template(account, external_id(request.app, eid))
        slug = "".join(ch if ch.isalnum() else "-" for ch in eng["client"]).strip("-")[:40].lower()
        return Response(
            body,
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": f'attachment; filename="grc-agent-read-only-{slug}.yaml"'
            },
        )
