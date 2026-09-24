"""Connector pages, evidence, notifications and intake/evidence contradiction checks."""

# No "from __future__ import annotations" here: the routes are defined inside register()
# with dependency types imported there, and FastAPI must be able to resolve them.

import json
import logging
import sqlite3
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from grc_agent.connectors import ConnectorError, by_category
from grc_agent.connectors.secrets import mask
from grc_agent.kpis.citations import normalize_citation
from grc_agent.web import db

log = logging.getLogger(__name__)


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
        if c is None or c.kind != "notify":
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
        return render(
            request,
            "connector_new.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="connectors",
            c=_connector(request, type),
        )

    @app.post("/engagements/{eid}/connectors")
    async def connector_create(eid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        c = _connector(request, str(form.get("connector", "")))
        config, secrets, missing = {}, {}, []
        for f in c.fields:
            raw = str(form.get(f.name, ""))
            value = raw.strip() if not f.multiline else raw.strip("\n")
            if not value.strip():
                if f.required:
                    missing.append(f.label)
                continue
            (secrets if f.secret else config)[f.name] = value[:20_000]
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
            row = _connection(conn, eid, cur.lastrowid)
            await _sync(request, conn, row, user)
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
