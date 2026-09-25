"""The client data-flow map: page, live JSON, custom systems and the mitigation plan."""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import csv
import io
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from grc_agent import dataflow
from grc_agent.web import db

STAGE_CHOICES = [(k, label) for k, label in dataflow.STAGES]


def safe_cell(value: object) -> object:
    """Spreadsheet apps run a cell starting with = + - @ as a formula; make it plain text."""
    text = str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def flow_for(request: Request, conn: sqlite3.Connection, eng: sqlite3.Row) -> dict:
    from grc_agent.web.analyst import flow_for as build

    return build(conn, request.app, eng)


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

    def agent_engagement(conn, eid):
        eng = get_engagement(conn, eid)
        if eng["mode"] != "agent":
            raise HTTPException(status_code=400, detail="Manual engagements have no data-flow map.")
        return eng

    @app.get("/dataflows")
    def dataflow_index(request: Request, user: User, conn: Conn):
        rows = conn.execute(
            "SELECT * FROM engagements WHERE mode = 'agent' ORDER BY id DESC"
        ).fetchall()
        maps = [{"eng": e, "flow": flow_for(request, conn, e)} for e in rows]
        return render(request, "dataflows.html", maps=maps)

    @app.get("/engagements/{eid}/dataflow")
    def dataflow_page(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        return render(
            request,
            "dataflow.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="dataflow",
            flow=flow_for(request, conn, eng),
            stage_choices=STAGE_CHOICES,
            locations=dataflow.LOCATIONS,
        )

    @app.get("/engagements/{eid}/dataflow.json")
    def dataflow_json(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        return JSONResponse(flow_for(request, conn, eng), headers={"Cache-Control": "no-store"})

    @app.post("/engagements/{eid}/dataflow/nodes")
    async def dataflow_add(eid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        eng = agent_engagement(conn, eid)
        name = str(form.get("name", "")).strip()[:80]
        stage = str(form.get("stage", ""))
        location = str(form.get("location", "unknown"))
        if not name or stage not in dataflow.STAGE_INDEX or location not in dataflow.LOCATIONS:
            flash(request, "Give the system a name and pick where it sits in the flow.", "error")
            return redirect(f"/engagements/{eid}/dataflow")
        conn.execute(
            "INSERT INTO dataflow_nodes (engagement_id, name, stage, location, categories, source, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                eid,
                name,
                stage,
                location,
                str(form.get("categories", "")).strip()[:500],
                str(form.get("source", ""))[:60],
                user,
                db.now(),
            ),
        )
        db.audit(conn, user, "dataflow_node_added", eid, {"name": name})
        flash(request, f"Added {name} to {eng['client']}'s data flow.")
        return redirect(f"/engagements/{eid}/dataflow")

    @app.post("/engagements/{eid}/dataflow/nodes/{nid}/delete")
    async def dataflow_remove(eid: int, nid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        agent_engagement(conn, eid)
        row = conn.execute(
            "SELECT name FROM dataflow_nodes WHERE id = ? AND engagement_id = ?", (nid, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        conn.execute("DELETE FROM dataflow_nodes WHERE id = ?", (nid,))
        db.audit(conn, user, "dataflow_node_removed", eid, {"name": row["name"]})
        flash(request, f"Removed {row['name']}.")
        return redirect(f"/engagements/{eid}/dataflow")

    @app.get("/engagements/{eid}/dataflow/plan.csv")
    def dataflow_plan_csv(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        flow = flow_for(request, conn, eng)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Step", "Level", "Where in the flow", "Issue", "Detail", "Action", "Provision"])
        for i, p in enumerate(flow["plan"], 1):
            row = [i, p["level"], "; ".join(p["where"]), p["title"], p["detail"], p["action"]]
            w.writerow([safe_cell(c) for c in [*row, p["provision"]]])
        slug = "".join(ch if ch.isalnum() else "-" for ch in eng["client"]).strip("-").lower()[:40]
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="data-flow-plan-{slug}.csv"'},
        )
