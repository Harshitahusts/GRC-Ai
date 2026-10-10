"""The full DPDP obligations register: every duty in the Act and the Rules, per client.

The catalogue lives in grc_agent/obligations.py. Here each client records its own status
for each duty (compliant, partly, not, doesn't apply), with an owner, a note and a date to
look at it again. Until someone reviews a duty, its status is suggested from what GRC Flow
already knows: the intake answers (does it apply?) and the Controls tab for the
assessment obligations it is linked to.

Each visit also records the day's readiness, so the page can show a trend.
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import re
import sqlite3
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from grc_agent import obligations as cat
from grc_agent.web import access, board_pack, db
from grc_agent.web.ops_views import COUNTS_AS_EVIDENCE

# Controls tab status -> the status it suggests for the linked duties.
FROM_CONTROL = {
    "implemented": "compliant",
    "in_progress": "partial",
    "needs_review": "partial",
    "not_started": "non_compliant",
    "not_applicable": "not_applicable",
}


def _suggest(item: cat.Item, answers: dict, controls: dict, findings: dict) -> tuple[str, str]:
    """(status, why) for a duty nobody has reviewed yet."""
    applies = cat.scope_applies(item.scope, answers)
    if applies is False:
        return "not_applicable", "Your intake answers say this doesn't apply."
    if applies is None and item.scope in ("sdf", "consent_manager", "disability"):
        return "not_applicable", f"{cat.SCOPES[item.scope]}. Change it if it applies to you."
    linked = [controls[o] for o in item.links if o in controls]
    if linked:
        states = {FROM_CONTROL[s] for s in linked}
        if states == {"compliant"}:
            return "compliant", "From the Controls tab."
        if states <= {"not_applicable"}:
            return "not_applicable", "From the Controls tab."
        if "compliant" in states or "partial" in states:
            return "partial", "From the Controls tab."
        return "non_compliant", "From the Controls tab."
    gaps = [findings[o] for o in item.links if o in findings]
    if gaps:
        if all(g == "compliant" for g in gaps):
            return "compliant", "From the assessment findings."
        if any(g in ("gap", "open_item") for g in gaps):
            return "non_compliant", "The assessment found a gap."
    return "not_assessed", ""


def items_for(conn: sqlite3.Connection, eid: int) -> list[dict]:
    """Every duty with this client's status, evidence count and suggestion."""
    answers = {
        r["question_id"]: r["answer"]
        for r in conn.execute(
            "SELECT question_id, answer FROM intake_answers WHERE engagement_id = ?", (eid,)
        )
    }
    controls = {
        r["obligation_id"]: r["status"]
        for r in conn.execute(
            "SELECT obligation_id, status FROM controls WHERE engagement_id = ?", (eid,)
        )
    }
    findings = {
        r["obligation_id"]: r["status"]
        for r in conn.execute(
            "SELECT obligation_id, status FROM findings WHERE engagement_id = ?", (eid,)
        )
    }
    reviews = {
        r["item_id"]: dict(r)
        for r in conn.execute("SELECT * FROM obligation_reviews WHERE engagement_id = ?", (eid,))
    }
    files: dict[str, int] = {}
    for r in conn.execute(
        # Safe: the SQL text holds only names from this code; values are ? parameters.
        "SELECT obligation_id, COUNT(*) AS n FROM evidence_files WHERE engagement_id = ? "  # nosec B608  # noqa: S608
        f"AND status = 'current' AND obligation_id != '' AND {COUNTS_AS_EVIDENCE} "
        "GROUP BY obligation_id",
        (eid,),
    ):
        files[r["obligation_id"]] = r["n"]
    records: dict[str, int] = {}
    for r in conn.execute(
        "SELECT register, COUNT(*) AS n FROM records WHERE engagement_id = ? GROUP BY register",
        (eid,),
    ):
        records[r["register"]] = r["n"]
    today = date.today().isoformat()
    out = []
    for item in cat.CATALOGUE:
        rv = reviews.get(item.id)
        suggested, why = _suggest(item, answers, controls, findings)
        status = rv["status"] if rv else suggested
        review_date = rv["review_date"] if rv else ""
        out.append(
            {
                "item": item,
                "status": status,
                "label": cat.STATUSES[status],
                "badge": cat.STATUS_BADGE[status],
                "reviewed": rv is not None,
                "why": "" if rv else why,
                "owner": rv["owner"] if rv else "",
                "note": rv["note"] if rv else "",
                "review_date": review_date,
                "recheck_due": bool(review_date) and review_date <= today,
                "updated_by": rv["updated_by"] if rv else "",
                "updated_at": rv["updated_at"] if rv else "",
                "files": files.get(item.id, 0) + sum(files.get(o, 0) for o in item.links),
                "records": records.get(item.register, 0) if item.register else None,
                "penalty_label": cat.PENALTIES[item.penalty][0],
                "penalty_max": cat.crore(cat.PENALTIES[item.penalty][1]),
            }
        )
    return out


def summary(items: list[dict]) -> dict:
    applicable = [i for i in items if i["status"] != "not_applicable"]
    compliant = sum(i["status"] == "compliant" for i in applicable)
    partial = sum(i["status"] == "partial" for i in applicable)
    return {
        "total": len(items),
        "applicable": len(applicable),
        "compliant": compliant,
        "partial": partial,
        "non_compliant": sum(i["status"] == "non_compliant" for i in applicable),
        "not_assessed": sum(i["status"] == "not_assessed" for i in applicable),
        "na": len(items) - len(applicable),
        "recheck": sum(i["recheck_due"] for i in items),
        # A partly met duty counts half, so the score moves as work progresses.
        "pct": round(100 * (compliant + partial / 2) / len(applicable)) if applicable else 0,
    }


def record_day(conn: sqlite3.Connection, eid: int, sm: dict) -> None:
    """Keep today's readiness (overwritten through the day, so the last value stands)."""
    conn.execute(
        "INSERT INTO readiness_history (engagement_id, day, compliant, partial, applicable, pct) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT (engagement_id, day) DO UPDATE SET "
        "compliant = excluded.compliant, partial = excluded.partial, "
        "applicable = excluded.applicable, pct = excluded.pct",
        (
            eid,
            date.today().isoformat(),
            sm["compliant"],
            sm["partial"],
            sm["applicable"],
            sm["pct"],
        ),
    )


def trend(conn: sqlite3.Connection, eid: int, days: int = 90) -> list[dict]:
    rows = conn.execute(
        "SELECT day, pct FROM readiness_history WHERE engagement_id = ? ORDER BY day DESC LIMIT ?",
        (eid, days),
    ).fetchall()
    return [{"day": r["day"], "pct": r["pct"]} for r in reversed(rows)]


def sparkline(points: list[dict], width: int = 320, height: int = 60) -> str:
    """SVG polyline points for a 0-100 series."""
    if not points:
        return ""
    if len(points) == 1:
        points = [points[0], points[0]]
    step = width / (len(points) - 1)
    return " ".join(
        f"{round(i * step, 1)},{round(height - p['pct'] * height / 100, 1)}"
        for i, p in enumerate(points)
    )


def rechecks_due(conn: sqlite3.Connection, ids: set[int] | None) -> list[dict]:
    """Duties, controls and evidence files whose re-check date has come, across clients."""
    today = date.today().isoformat()
    out = []
    for r in conn.execute(
        "SELECT r.engagement_id, r.item_id, r.owner, r.review_date, e.client "
        "FROM obligation_reviews r JOIN engagements e ON e.id = r.engagement_id "
        "WHERE r.review_date != '' AND r.review_date <= ?",
        (today,),
    ):
        item = cat.BY_ID.get(r["item_id"])
        if item:
            out.append(
                {
                    "engagement_id": r["engagement_id"],
                    "client": r["client"],
                    "what": f"{item.ref}: {item.title}",
                    "kind": "Obligation",
                    "owner": r["owner"],
                    "due": r["review_date"],
                    "href": f"/engagements/{r['engagement_id']}/obligations#{item.id}",
                }
            )
    for r in conn.execute(
        "SELECT c.engagement_id, c.obligation_id, c.owner, c.review_date, e.client "
        "FROM controls c JOIN engagements e ON e.id = c.engagement_id "
        "WHERE c.review_date != '' AND c.review_date <= ?",
        (today,),
    ):
        out.append(
            {
                "engagement_id": r["engagement_id"],
                "client": r["client"],
                "what": r["obligation_id"],
                "kind": "Control",
                "owner": r["owner"],
                "due": r["review_date"],
                "href": f"/engagements/{r['engagement_id']}/controls#{r['obligation_id']}",
            }
        )
    for r in conn.execute(
        "SELECT f.engagement_id, f.title, f.uploaded_by, f.review_date, e.client "
        "FROM evidence_files f JOIN engagements e ON e.id = f.engagement_id "
        "WHERE f.status = 'current' AND f.review_date != '' AND f.review_date <= ?",
        (today,),
    ):
        out.append(
            {
                "engagement_id": r["engagement_id"],
                "client": r["client"],
                "what": r["title"],
                "kind": "Evidence",
                "owner": r["uploaded_by"],
                "due": r["review_date"],
                "href": f"/engagements/{r['engagement_id']}/evidence",
            }
        )
    out = access.only(out, ids)
    return sorted(out, key=lambda x: (x["due"], x["client"]))


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

    @app.get("/engagements/{eid}/obligations")
    def obligations_page(eid: int, request: Request, user: User, conn: Conn, show: str = ""):
        eng = get_engagement(conn, eid)
        items = items_for(conn, eid)
        sm = summary(items)
        record_day(conn, eid, sm)
        points = trend(conn, eid)
        shown = items
        if show in cat.STATUSES:
            shown = [i for i in items if i["status"] == show]
        elif show == "recheck":
            shown = [i for i in items if i["recheck_due"]]
        return render(
            request,
            "obligations.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="obligations",
            items=shown,
            all_count=len(items),
            sm=sm,
            show=show,
            exposure=cat.exposure(items),
            statuses=cat.STATUSES,
            scopes=cat.SCOPES,
            points=points,
            spark=sparkline(points),
            people=access.assignable(conn, eid),
            is_admin=access.leads(conn, user, eid),
        )

    @app.post("/engagements/{eid}/obligations/{item_id}")
    async def obligation_save(eid: int, item_id: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        item = cat.BY_ID.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Unknown obligation")
        back = f"/engagements/{eid}/obligations#{item_id}"
        status = str(form.get("status", ""))
        if status not in cat.STATUSES:
            raise HTTPException(status_code=400, detail="Unknown status")
        owner = str(form.get("owner", "")).strip()[:80]
        note = str(form.get("note", "")).strip()[:1000]
        review_date = str(form.get("review_date", "")).strip()[:10]
        if review_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_date):
            flash(request, "Use a date for the re-check date.", "error")
            return redirect(back)
        if status == "not_applicable":
            if not note:
                flash(request, "Say why this doesn't apply, in the note.", "error")
                return redirect(back)
            if not access.leads(conn, user, eid):
                flash(request, "Only an admin or manager can mark a duty as not applying.", "error")
                return redirect(back)
        if status == "compliant" and not note:
            has_evidence = any(i["files"] for i in items_for(conn, eid) if i["item"].id == item_id)
            if not has_evidence:
                flash(
                    request,
                    "To mark it compliant, attach evidence or describe how it's done.",
                    "error",
                )
                return redirect(back)
        conn.execute(
            "INSERT INTO obligation_reviews (engagement_id, item_id, status, owner, note, "
            "review_date, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (engagement_id, item_id) DO UPDATE SET status = excluded.status, "
            "owner = excluded.owner, note = excluded.note, review_date = excluded.review_date, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (eid, item_id, status, owner, note, review_date, user, db.now()),
        )
        db.audit(
            conn,
            user,
            "obligation_reviewed",
            eid,
            {"item": item_id, "ref": item.ref, "status": status, "label": cat.STATUSES[status]},
        )
        flash(request, f"{item.ref}: {cat.STATUSES[status]}.")
        return redirect(back)

    def _filename(eng, what: str, ext: str) -> str:
        client = "".join(c if c.isalnum() else "-" for c in eng["client"]).strip("-")[:40]
        return f"{client}-{what}.{ext}"

    @app.get("/engagements/{eid}/board-pack.zip")
    def board_pack_zip(eid: int, request: Request, user: User, conn: Conn):
        eng = get_engagement(conn, eid)
        data = board_pack.build(conn, request.app, eng, user)
        name = _filename(eng, "board-pack", "zip")
        db.audit(conn, user, "board_pack_exported", eid, {"bytes": len(data)})
        return Response(
            data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/engagements/{eid}/r/breaches/{rid}/board-report.txt")
    def breach_board_report(eid: int, rid: int, request: Request, user: User, conn: Conn):
        eng = get_engagement(conn, eid)
        rec = conn.execute(
            "SELECT * FROM records WHERE id = ? AND engagement_id = ? AND register = 'breaches'",
            (rid, eid),
        ).fetchone()
        if rec is None:
            raise HTTPException(status_code=404, detail="Breach not found")
        db.audit(conn, user, "breach_report_drafted", eid, {"ref": rec["ref"]})
        return Response(
            board_pack.breach_report(eng, rec),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_filename(eng, rec["ref"] + "-board-report", "txt")}"'
                ),
                "Cache-Control": "no-store",
            },
        )
