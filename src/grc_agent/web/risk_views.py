"""The engagement risk register page and its edits."""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import re
from datetime import date

from fastapi import FastAPI, HTTPException, Request

from grc_agent import risk
from grc_agent.web import db
from grc_agent.web.analyst import risks_for


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
            raise HTTPException(status_code=400, detail="Manual engagements have no risk register.")
        return eng

    @app.get("/engagements/{eid}/risks")
    def risks_page(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        risks = risks_for(conn, request.app, eid)
        return render(
            request,
            "risks.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="risks",
            risks=risks,
            heat=risk.heatmap(risks),
            rs=risk.summary(risks),
            treatments=risk.TREATMENTS,
            statuses=risk.STATUSES,
            today=date.today().isoformat(),
        )

    @app.post("/engagements/{eid}/risks")
    async def risk_edit(eid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        agent_engagement(conn, eid)
        key = str(form.get("risk_key", ""))
        current = {r.key: r for r in risks_for(conn, request.app, eid)}
        if key not in current:
            raise HTTPException(status_code=404, detail="Unknown risk")

        def score(name: str) -> int:
            value = str(form.get(name, ""))
            return (
                int(value)
                if value.isdigit() and 1 <= int(value) <= 5
                else getattr(current[key], name)
            )

        treatment = str(form.get("treatment", "mitigate"))
        status = str(form.get("status", "open"))
        due = str(form.get("due", "")).strip()
        notes = str(form.get("notes", "")).strip()[:1000]
        if treatment not in risk.TREATMENTS or status not in risk.STATUSES:
            raise HTTPException(status_code=400, detail="Invalid treatment or status")
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            flash(request, "Use a date for the due date.", "error")
            return redirect(f"/engagements/{eid}/risks")
        if treatment == "accept" and not notes:
            flash(request, "Accepting a risk needs a reason. Add it in the notes.", "error")
            return redirect(f"/engagements/{eid}/risks#{key}")
        values = (
            score("likelihood"),
            score("impact"),
            treatment,
            str(form.get("owner", "")).strip()[:80],
            due,
            status,
            notes,
        )
        conn.execute(
            "INSERT INTO risk_edits (engagement_id, risk_key, likelihood, impact, treatment, "
            "owner, due, status, notes, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (engagement_id, risk_key) DO UPDATE SET likelihood = excluded.likelihood, "
            "impact = excluded.impact, treatment = excluded.treatment, owner = excluded.owner, "
            "due = excluded.due, status = excluded.status, notes = excluded.notes, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (eid, key, *values, user, db.now()),
        )
        db.audit(
            conn,
            user,
            "risk_updated",
            eid,
            {"risk": key, "title": current[key].title, "status": status, "treatment": treatment},
        )
        flash(request, f"Saved: {current[key].title}")
        return redirect(f"/engagements/{eid}/risks")
