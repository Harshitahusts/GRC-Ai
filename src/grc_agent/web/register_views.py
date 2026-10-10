"""Routes shared by every register (tasks, consent, requests, breaches, vendors, DPIAs, policies).

A register is declared in registers.py; these routes give it a list with filters, a
form, a detail page with its status workflow, a full change history, comments and
evidence attachments, and a CSV export. Records always belong to one engagement and
every query is scoped to it.
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import json
import sqlite3
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request

from grc_agent.web import access, db
from grc_agent.web.registers import (
    REGISTERS,
    RegisterSpec,
    clean,
    due_for,
    is_overdue,
    status_problems,
)

TONE_BADGE = {
    "todo": "badge-none",
    "active": "badge-warn",
    "done": "badge-pass",
    "bad": "badge-fail",
}


# ---------------------------------------------------------------- queries


def next_ref(conn: sqlite3.Connection, eid: int, spec: RegisterSpec) -> str:
    n = conn.execute(
        "SELECT COUNT(*) FROM records WHERE engagement_id = ? AND register = ?", (eid, spec.key)
    ).fetchone()[0]
    while True:
        n += 1
        ref = f"{spec.prefix}-{n:03d}"
        if not conn.execute(
            "SELECT 1 FROM records WHERE engagement_id = ? AND register = ? AND ref = ?",
            (eid, spec.key, ref),
        ).fetchone():
            return ref


def view(row: sqlite3.Row, spec: RegisterSpec, now: datetime | None = None) -> dict:
    item = dict(row)
    item["data"] = json.loads(row["data_json"])
    st = spec.status(row["status"]) if row["status"] in spec.status_keys else None
    item["status_label"] = st.label if st else row["status"]
    item["status_badge"] = TONE_BADGE[st.tone] if st else "badge-none"
    item["closed"] = bool(st and st.closed)
    item["overdue"] = is_overdue(row["due"], row["status"], spec, now)
    return item


def records(
    conn: sqlite3.Connection, eid: int, spec: RegisterSpec, show: str = "open", q: str = ""
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM records WHERE engagement_id = ? AND register = ? ORDER BY id DESC",
        (eid, spec.key),
    ).fetchall()
    items = [view(r, spec) for r in rows]
    if show == "open":
        items = [i for i in items if not i["closed"]]
    elif show == "overdue":
        items = [i for i in items if i["overdue"]]
    elif show in spec.status_keys:
        items = [i for i in items if i["status"] == show]
    if q:
        needle = q.lower()
        items = [
            i
            for i in items
            if needle
            in (i["title"] + i["ref"] + i["owner"] + i["obligation_id"] + i["data_json"]).lower()
        ]
    # Overdue first, then soonest due, then newest.
    return sorted(items, key=lambda i: (not i["overdue"], i["due"] or "9999", -i["id"]))


def counts(conn: sqlite3.Connection, eid: int | None = None) -> dict[str, dict[str, int]]:
    """Open and overdue records per register, for one engagement or all of them."""
    where, args = ("WHERE engagement_id = ?", (eid,)) if eid is not None else ("", ())
    out = {k: {"open": 0, "overdue": 0, "total": 0} for k in REGISTERS}
    # Safe: the SQL text holds only names from this code; values are ? parameters.
    for r in conn.execute(f"SELECT register, status, due FROM records {where}", args):  # nosec B608  # noqa: S608
        spec = REGISTERS.get(r["register"])
        if not spec:
            continue
        c = out[spec.key]
        c["total"] += 1
        if r["status"] in spec.open_statuses:
            c["open"] += 1
            c["overdue"] += is_overdue(r["due"], r["status"], spec)
    return out


def queue(conn: sqlite3.Connection, limit: int = 50, ids: set[int] | None = None) -> list[dict]:
    """Open work across every client (or only ``ids``), overdue and soonest first."""
    rows = conn.execute(
        "SELECT r.*, e.client FROM records r JOIN engagements e ON e.id = r.engagement_id"
    ).fetchall()
    items = []
    for r in rows:
        if ids is not None and r["engagement_id"] not in ids:
            continue
        spec = REGISTERS.get(r["register"])
        if spec and r["status"] in spec.open_statuses:
            item = view(r, spec)
            item["client"] = r["client"]
            item["spec"] = spec
            items.append(item)
    items.sort(key=lambda i: (not i["overdue"], i["due"] or "9999", i["register"] != "breaches"))
    return items[:limit]


def events(conn: sqlite3.Connection, rid: int) -> list[dict]:
    out = []
    for e in conn.execute(
        "SELECT * FROM record_events WHERE record_id = ? ORDER BY id DESC", (rid,)
    ):
        item = dict(e)
        item["detail"] = json.loads(e["detail_json"])
        out.append(item)
    return out


def add_event(conn, rid: int, eid: int, user: str, kind: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO record_events (record_id, engagement_id, at, username, kind, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (rid, eid, db.now(), user, kind, json.dumps(detail)),
    )


def create_record(
    conn: sqlite3.Connection,
    eid: int,
    spec: RegisterSpec,
    data: dict,
    user: str,
    owner: str = "",
    due: str = "",
    obligation_id: str = "",
    status: str | None = None,
) -> int:
    ref = next_ref(conn, eid, spec)
    status = status or spec.statuses[0].key
    cur = conn.execute(
        "INSERT INTO records (engagement_id, register, ref, title, status, owner, due, "
        "obligation_id, data_json, created_by, created_at, updated_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            eid,
            spec.key,
            ref,
            data.get(spec.title_field, "") or ref,
            status,
            owner,
            due_for(spec, data, due),
            obligation_id or (spec.obligations[0] if spec.obligations else ""),
            json.dumps(data),
            user,
            db.now(),
            user,
            db.now(),
        ),
    )
    rid = cur.lastrowid
    add_event(conn, rid, eid, user, "created", {"status": status})
    db.audit(
        conn,
        user,
        "record_created",
        eid,
        {"register": spec.key, "ref": ref, "title": data.get(spec.title_field, "")},
    )
    return rid


# ---------------------------------------------------------------- routes


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
    from grc_agent.web.discovery_views import _csv

    def spec_for(key: str) -> RegisterSpec:
        if key not in REGISTERS:
            raise HTTPException(status_code=404, detail="No such register")
        return REGISTERS[key]

    def agent_engagement(conn, eid):
        return get_engagement(conn, eid)

    def open_engagement(conn, eid):
        # Delivery locks the assessment and its documents, not the day-to-day records:
        # breaches, requests and the rest keep their legal clocks after delivery.
        return agent_engagement(conn, eid)

    def get_record(conn, eid: int, spec: RegisterSpec, rid: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ? AND engagement_id = ? AND register = ?",
            (rid, eid, spec.key),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Record not found")
        return row

    def people(conn, eid: int) -> list[str]:
        return access.assignable(conn, eid)

    def common(request, conn, eng, spec):
        return dict(
            eng=eng,
            s=_summary(conn, eng),
            tab=spec.key,
            spec=spec,
            obligations=request.app.state.register.obligations,
            people=people(conn, eng["id"]),
        )

    def obligation_ok(request, value: str) -> str:
        ids = {o.id for o in request.app.state.register.obligations}
        return value if value in ids else ""

    # Registered before the list route so "tasks.csv" isn't read as a register name.
    @app.get("/engagements/{eid}/r/{key}.csv")
    def register_csv(eid: int, key: str, request: Request, user: User, conn: Conn):
        spec = spec_for(key)
        eng = agent_engagement(conn, eid)
        items = records(conn, eid, spec, "all")
        header = ["Ref", "Status", "Owner", spec.due_label, "Obligation"] + [
            f.label for f in spec.fields
        ]
        body = [
            [i["ref"], i["status_label"], i["owner"], i["due"], i["obligation_id"]]
            + [i["data"].get(f.name, "") for f in spec.fields]
            for i in items
        ]
        return _csv(eng, spec.key, header, body)

    @app.get("/engagements/{eid}/r/{key}")
    def register_list(
        eid: int,
        key: str,
        request: Request,
        user: User,
        conn: Conn,
        show: str = "open",
        q: str = "",
    ):
        spec = spec_for(key)
        eng = agent_engagement(conn, eid)
        return render(
            request,
            "register_list.html",
            **common(request, conn, eng, spec),
            items=records(conn, eid, spec, show, q.strip()[:80]),
            show=show,
            q=q,
            c=counts(conn, eid)[spec.key],
        )

    @app.get("/engagements/{eid}/r/{key}/new")
    def register_new(eid: int, key: str, request: Request, user: User, conn: Conn):
        spec = spec_for(key)
        eng = open_engagement(conn, eid)
        qp = request.query_params
        prefill = {f.name: qp.get(f.name, "")[: f.maxlen] for f in spec.fields}
        return render(
            request,
            "register_form.html",
            **common(request, conn, eng, spec),
            item=None,
            values=prefill,
            owner=qp.get("owner", "")[:80],
            due=qp.get("due", "")[:10],
            obligation_id=obligation_ok(request, qp.get("obligation", "")),
            source=qp.get("source", "")[:200],
            errors=[],
        )

    @app.post("/engagements/{eid}/r/{key}")
    async def register_create(eid: int, key: str, request: Request, user: User, conn: Conn):
        spec = spec_for(key)
        form = await form_with_csrf(request)
        eng = open_engagement(conn, eid)
        data, errors = clean(spec, form)
        owner = str(form.get("owner", "")).strip()[:80]
        due = str(form.get("due", "")).strip()[:10]
        obligation_id = obligation_ok(request, str(form.get("obligation_id", "")))
        if errors:
            return render(
                request,
                "register_form.html",
                status_code=400,
                **common(request, conn, eng, spec),
                item=None,
                values=data,
                owner=owner,
                due=due,
                obligation_id=obligation_id,
                source=str(form.get("source", ""))[:200],
                errors=errors,
            )
        rid = create_record(conn, eid, spec, data, user, owner, due, obligation_id)
        source = str(form.get("source", "")).strip()[:200]
        if source:
            add_event(conn, rid, eid, user, "comment", {"text": f"Created from: {source}"})
        flash(request, f"Added {spec.singular} {data.get(spec.title_field) or ''}.".strip())
        return redirect(f"/engagements/{eid}/r/{key}/{rid}")

    @app.get("/engagements/{eid}/r/{key}/{rid}")
    def register_detail(eid: int, key: str, rid: int, request: Request, user: User, conn: Conn):
        spec = spec_for(key)
        eng = agent_engagement(conn, eid)
        item = view(get_record(conn, eid, spec, rid), spec)
        files = conn.execute(
            "SELECT * FROM evidence_files WHERE record_id = ? AND engagement_id = ? "
            "AND status = 'current' ORDER BY id DESC",
            (rid, eid),
        ).fetchall()
        blocked = {
            s.key: status_problems(spec, s.key, item["data"])
            for s in spec.statuses
            if s.key != item["status"]
        }
        return render(
            request,
            "register_detail.html",
            **common(request, conn, eng, spec),
            item=item,
            history=events(conn, rid),
            files=files,
            blocked=blocked,
        )

    @app.post("/engagements/{eid}/r/{key}/{rid}/edit")
    async def register_edit(eid: int, key: str, rid: int, request: Request, user: User, conn: Conn):
        spec = spec_for(key)
        form = await form_with_csrf(request)
        eng = open_engagement(conn, eid)
        row = get_record(conn, eid, spec, rid)
        data, errors = clean(spec, form)
        owner = str(form.get("owner", "")).strip()[:80]
        due = str(form.get("due", "")).strip()[:10]
        # A form without the obligation field leaves the link as it was.
        obligation_id = (
            obligation_ok(request, str(form.get("obligation_id", "")))
            if "obligation_id" in form
            else row["obligation_id"]
        )
        if errors:
            return render(
                request,
                "register_form.html",
                status_code=400,
                **common(request, conn, eng, spec),
                item=view(row, spec),
                values=data,
                owner=owner,
                due=due,
                obligation_id=obligation_id,
                source="",
                errors=errors,
            )
        # The current status must still hold with the edited values.
        problems = status_problems(spec, row["status"], data)
        if problems:
            flash(request, "Not saved. " + " ".join(problems), "error")
            return redirect(f"/engagements/{eid}/r/{key}/{rid}")
        old = json.loads(row["data_json"])
        changed = sorted(
            [spec.field(k).label for k in data if data[k] != old.get(k, "")]
            + (["Owner"] if owner != row["owner"] else [])
            + (["Obligation"] if obligation_id != row["obligation_id"] else [])
        )
        conn.execute(
            "UPDATE records SET title = ?, owner = ?, due = ?, obligation_id = ?, data_json = ?, "
            "updated_by = ?, updated_at = ? WHERE id = ?",
            (
                data.get(spec.title_field, "") or row["ref"],
                owner,
                due_for(spec, data, due),
                obligation_id,
                json.dumps(data),
                user,
                db.now(),
                rid,
            ),
        )
        if changed:
            add_event(conn, rid, eid, user, "edited", {"fields": changed})
            db.audit(
                conn,
                user,
                "record_updated",
                eid,
                {"register": key, "ref": row["ref"], "fields": changed},
            )
        flash(request, "Saved." if changed else "No changes.")
        return redirect(f"/engagements/{eid}/r/{key}/{rid}")

    @app.post("/engagements/{eid}/r/{key}/{rid}/status")
    async def register_status(
        eid: int, key: str, rid: int, request: Request, user: User, conn: Conn
    ):
        spec = spec_for(key)
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        row = get_record(conn, eid, spec, rid)
        status = str(form.get("status", ""))
        note = str(form.get("note", "")).strip()[:500]
        if status not in spec.status_keys:
            raise HTTPException(status_code=400, detail="Unknown status")
        if status == row["status"]:
            return redirect(f"/engagements/{eid}/r/{key}/{rid}")
        problems = status_problems(spec, status, json.loads(row["data_json"]))
        if problems:
            flash(request, " ".join(problems), "error")
            return redirect(f"/engagements/{eid}/r/{key}/{rid}")
        conn.execute(
            "UPDATE records SET status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
            (status, user, db.now(), rid),
        )
        add_event(
            conn, rid, eid, user, "status", {"from": row["status"], "to": status, "note": note}
        )
        db.audit(
            conn,
            user,
            "record_status",
            eid,
            {
                "register": key,
                "ref": row["ref"],
                "title": row["title"],
                "to": status,
                "label": spec.status(status).label,
                "closed": spec.status(status).closed,
            },
        )
        flash(request, f"{row['ref']} is now “{spec.status(status).label}”.")
        return redirect(f"/engagements/{eid}/r/{key}/{rid}")

    @app.post("/engagements/{eid}/r/{key}/{rid}/comment")
    async def register_comment(
        eid: int, key: str, rid: int, request: Request, user: User, conn: Conn
    ):
        spec = spec_for(key)
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        row = get_record(conn, eid, spec, rid)
        text = str(form.get("text", "")).strip()[:2000]
        if not text:
            flash(request, "Write a comment first.", "error")
            return redirect(f"/engagements/{eid}/r/{key}/{rid}")
        add_event(conn, rid, eid, user, "comment", {"text": text})
        conn.execute(
            "UPDATE records SET updated_by = ?, updated_at = ? WHERE id = ?",
            (user, db.now(), rid),
        )
        db.audit(conn, user, "record_comment", eid, {"register": key, "ref": row["ref"]})
        return redirect(f"/engagements/{eid}/r/{key}/{rid}#history")
