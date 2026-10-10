"""Personal data discovery and the data inventory, per engagement.

Flow: upload a CSV or JSON file -> a background scan finds fields that look like personal
data -> a person confirms or rejects each finding -> confirmed findings become inventory
records -> the consultant fills in purpose, legal basis, retention and owner.

The uploaded file is parsed in memory and never written to disk. Only field-level
metadata and masked value shapes are stored.
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import csv
import io
import json
import logging
import sqlite3
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from grc_agent.discovery import catalog, samples
from grc_agent.discovery.engine import get_engine, presidio_installed
from grc_agent.discovery.scanner import MAX_BYTES, ScanInputError, Table, read_table, scan_table
from grc_agent.web import db
from grc_agent.web.dataflow_views import safe_cell

log = logging.getLogger(__name__)

SAMPLE_FILES = {
    "customers_sample.csv": ("text/csv", "E-commerce customers (CSV)"),
    "clinic_patients_sample.json": ("application/json", "Clinic patients (JSON)"),
}


def sample_bytes(name: str) -> bytes:
    return (Path(samples.__file__).parent / name).read_bytes()


# Inventory fields a consultant fills in, with the label used in gap messages.
REQUIRED = {
    "purpose": "purpose",
    "principals": "whose data it is",
    "legal_basis": "legal basis",
    "retention": "retention period",
    "owner": "owner",
}
EDITABLE = (*REQUIRED, "storage_location", "recipients")
LIMITS = {"purpose": 300, "retention": 120, "storage_location": 120, "recipients": 300}


# ---------------------------------------------------------------- background scan


def run_scan(db_path: Path, scan_id: int, table: Table) -> None:
    """Scan a parsed table and store findings. Runs after the upload request returns."""
    conn = db.connect(db_path)
    try:
        job = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (scan_id,)).fetchone()
        eid = job["engagement_id"]
        engine = get_engine()
        conn.execute(
            "UPDATE scan_jobs SET status = 'running', engine = ?, started_at = ? WHERE id = ?",
            (engine.name, db.now(), scan_id),
        )
        conn.commit()

        def progress(done: int, total: int) -> None:
            conn.execute(
                "UPDATE scan_jobs SET progress = ? WHERE id = ?",
                (int(100 * done / total), scan_id),
            )
            conn.commit()

        results = scan_table(table, engine, progress)
        in_inventory = {
            r["field"]
            for r in conn.execute(
                "SELECT field FROM data_inventory WHERE engagement_id = ? AND source_name = ?",
                (eid, job["source_name"]),
            )
        }
        # A rescan of the same source replaces its findings still awaiting review.
        conn.execute(
            "UPDATE scan_findings SET status = 'superseded' WHERE engagement_id = ? "
            "AND source_name = ? AND status = 'pending'",
            (eid, job["source_name"]),
        )
        for r in results:
            known = r.column in in_inventory
            conn.execute(
                "INSERT INTO scan_findings (scan_id, engagement_id, source_name, column_name, "
                "kind, category, risk, confidence, match_ratio, sampled, entities_json, "
                "shapes_json, minors, status, note, reviewed_by, reviewed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    eid,
                    job["source_name"],
                    r.column,
                    r.primary,
                    r.category,
                    r.risk,
                    r.confidence,
                    r.match_ratio,
                    r.sampled,
                    json.dumps(r.entities),
                    json.dumps(r.shapes),
                    r.minors,
                    "confirmed" if known else "pending",
                    "Already in the inventory" if known else "",
                    "scanner" if known else None,
                    db.now() if known else None,
                ),
            )
        conn.execute(
            "UPDATE scan_jobs SET status = 'done', progress = 100, personal_fields = ?, "
            "finished_at = ? WHERE id = ?",
            (len(results), db.now(), scan_id),
        )
        db.audit(
            conn,
            job["created_by"],
            "scan_completed",
            eid,
            {
                "scan": scan_id,
                "source": job["source_name"],
                "fields": len(results),
                "high_risk": sum(r.risk == "high" for r in results),
                "children": sum(r.minors for r in results),
                "engine": engine.name,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("Scan %s failed", scan_id)
        conn.execute(
            "UPDATE scan_jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (
                "The scanner hit an internal error. Upload the file again to retry.",
                db.now(),
                scan_id,
            ),
        )
        row = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (scan_id,)).fetchone()
        if row:
            db.audit(
                conn,
                row["created_by"],
                "scan_failed",
                row["engagement_id"],
                {"scan": scan_id, "source": row["source_name"]},
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- queries


def inventory_rows(conn: sqlite3.Connection, eid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM data_inventory WHERE engagement_id = ? ORDER BY source_name, "
        "CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, field",
        (eid,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["label"] = catalog.KINDS[r["kind"]].label if r["kind"] in catalog.KINDS else r["kind"]
        item["gaps"] = [label for key, label in REQUIRED.items() if not r[key]]
        item["obligations"] = catalog.obligations_for(
            r["category"], r["legal_basis"], bool(r["children"])
        )
        out.append(item)
    return out


def summary(conn: sqlite3.Connection, eid: int | None = None, ids: set[int] | None = None) -> dict:
    """Counts for the dashboard and tabs. eid=None sums over every engagement, or over
    ``ids`` when given (the clients a person can open)."""
    if eid is not None:
        where, args = "WHERE engagement_id = ?", (eid,)
    elif ids is not None:
        marks = ",".join("?" for _ in ids) or "NULL"
        where, args = f"WHERE engagement_id IN ({marks})", tuple(sorted(ids))
    else:
        where, args = "", ()
    pending = conn.execute(
        # Safe: the SQL text holds only names from this code; values are ? parameters.
        f"SELECT COUNT(*) FROM scan_findings {where}{' AND' if where else ' WHERE'} "  # nosec B608  # noqa: S608
        "status = 'pending'",
        args,
    ).fetchone()[0]
    # Safe: the SQL text holds only names from this code; values are ? parameters.
    inv = conn.execute(f"SELECT * FROM data_inventory {where}", args).fetchall()  # nosec B608  # noqa: S608
    incomplete = sum(1 for r in inv if any(not r[k] for k in REQUIRED))
    scans = conn.execute(
        # Safe: the SQL text holds only names from this code; values are ? parameters.
        "SELECT COUNT(*), SUM(CASE WHEN status IN ('queued', 'running') THEN 1 ELSE 0 END) "  # nosec B608  # noqa: S608
        f"FROM scan_jobs {where}",
        args,
    ).fetchone()
    # Children's data the reviewer hasn't decided on yet counts too: it is the most
    # urgent thing to look at.
    pending_children = conn.execute(
        # Safe: the SQL text holds only names from this code; values are ? parameters.
        f"SELECT COUNT(*) FROM scan_findings {where}{' AND' if where else ' WHERE'} "  # nosec B608  # noqa: S608
        "status = 'pending' AND minors > 0",
        args,
    ).fetchone()[0]
    return {
        "pending": pending,
        "inventory": len(inv),
        "incomplete": incomplete,
        "complete": len(inv) - incomplete,
        "high_risk": sum(1 for r in inv if r["risk"] == "high"),
        "children": sum(1 for r in inv if r["children"]) + pending_children,
        "scans": scans[0] or 0,
        "running": scans[1] or 0,
    }


def by_engagement(conn: sqlite3.Connection, ids: set[int] | None = None) -> list[dict]:
    """Per-client discovery status for the dashboard, busiest first (only ``ids`` if given)."""
    rows = conn.execute(
        "SELECT e.id, e.client, "
        "(SELECT COUNT(*) FROM scan_findings f WHERE f.engagement_id = e.id "
        " AND f.status = 'pending') AS pending, "
        "(SELECT COUNT(*) FROM data_inventory i WHERE i.engagement_id = e.id) AS inventory "
        "FROM engagements e"
    ).fetchall()
    out = [
        dict(r)
        for r in rows
        if (r["pending"] or r["inventory"]) and (ids is None or r["id"] in ids)
    ]
    for r in out:
        r["incomplete"] = summary(conn, r["id"])["incomplete"]
    return sorted(out, key=lambda r: (-r["pending"], -r["incomplete"], r["client"]))


def _finding_view(r: sqlite3.Row) -> dict:
    item = dict(r)
    item["label"] = catalog.KINDS[r["kind"]].label if r["kind"] in catalog.KINDS else r["kind"]
    item["entities"] = [
        (catalog.KINDS[k].label if k in catalog.KINDS else k, v)
        for k, v in json.loads(r["entities_json"]).items()
    ]
    item["shapes"] = json.loads(r["shapes_json"])
    return item


def _add_to_inventory(conn: sqlite3.Connection, finding: sqlite3.Row, user: str) -> None:
    conn.execute(
        "INSERT INTO data_inventory (engagement_id, finding_id, source_name, field, kind, "
        "category, risk, children, principals, created_by, created_at, updated_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (engagement_id, source_name, field) "
        "DO UPDATE SET finding_id = excluded.finding_id, kind = excluded.kind, "
        "category = excluded.category, risk = excluded.risk, "
        "children = CASE WHEN excluded.children > data_inventory.children "
        "THEN excluded.children ELSE data_inventory.children END",
        (
            finding["engagement_id"],
            finding["id"],
            finding["source_name"],
            finding["column_name"],
            finding["kind"],
            finding["category"],
            finding["risk"],
            1 if finding["minors"] else 0,
            "Children" if finding["minors"] else "",
            user,
            db.now(),
            user,
            db.now(),
        ),
    )


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

    def agent_engagement(conn, eid):
        return get_engagement(conn, eid)

    def open_engagement(conn, eid):
        # Delivery locks the assessment and its documents, not the day-to-day records:
        # breaches, requests and the rest keep their legal clocks after delivery.
        return agent_engagement(conn, eid)

    def get_finding(conn, eid: int, fid: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM scan_findings WHERE id = ? AND engagement_id = ?", (fid, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Finding not found")
        return row

    def get_item(conn, eid: int, iid: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM data_inventory WHERE id = ? AND engagement_id = ?", (iid, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Inventory record not found")
        return row

    # ---- discovery

    @app.get("/engagements/{eid}/discovery")
    def discovery_page(eid: int, request: Request, user: User, conn: Conn, show: str = "pending"):
        eng = agent_engagement(conn, eid)
        scans = conn.execute(
            "SELECT * FROM scan_jobs WHERE engagement_id = ? ORDER BY id DESC LIMIT 20", (eid,)
        ).fetchall()
        statuses = ("pending", "confirmed", "rejected", "superseded", "all")
        show = show if show in statuses else "pending"
        rows = conn.execute(
            # Safe: the SQL text holds only names from this code; values are ? parameters.
            "SELECT * FROM scan_findings WHERE engagement_id = ? "  # nosec B608  # noqa: S608
            + ("" if show == "all" else "AND status = ? ")
            + "ORDER BY source_name, CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id",
            (eid,) if show == "all" else (eid, show),
        ).fetchall()
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM scan_findings WHERE engagement_id = ? "
                "GROUP BY status",
                (eid,),
            ).fetchall()
        )
        engine = get_engine()
        return render(
            request,
            "discovery.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="personal",
            subtab="discovery",
            ds=summary(conn, eid),
            scans=scans,
            findings=[_finding_view(r) for r in rows],
            show=show,
            counts=counts,
            engine=engine,
            presidio_installed=presidio_installed(),
            max_mb=MAX_BYTES // (1024 * 1024),
            sample_files=SAMPLE_FILES,
            running=any(s["status"] in ("queued", "running") for s in scans),
        )

    @app.post("/engagements/{eid}/discovery/scan")
    async def discovery_scan(
        eid: int, request: Request, user: User, conn: Conn, background: BackgroundTasks
    ):
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        upload = form.get("file")
        source = str(form.get("source_name", "")).strip()[:80]
        if not isinstance(upload, UploadFile) or not upload.filename:
            flash(request, "Choose a CSV or JSON file to scan.", "error")
            return redirect(f"/engagements/{eid}/discovery")
        data = await upload.read(MAX_BYTES + 1)
        try:
            table = read_table(upload.filename, data)
        except ScanInputError as e:
            flash(request, f"Couldn't scan {upload.filename}: {e}", "error")
            return redirect(f"/engagements/{eid}/discovery")
        finally:
            await upload.close()
        filename = Path(upload.filename).name[:120]
        source = source or Path(filename).stem
        cur = conn.execute(
            "INSERT INTO scan_jobs (engagement_id, source_name, filename, file_kind, size_bytes, "
            "rows, columns, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                eid,
                source,
                filename,
                table.kind,
                len(data),
                table.rows,
                len(table.columns),
                user,
                db.now(),
            ),
        )
        scan_id = cur.lastrowid
        db.audit(conn, user, "scan_started", eid, {"scan": scan_id, "source": source})
        conn.commit()  # the background scan reads this row on its own connection
        del data
        background.add_task(run_scan, request.app.state.db_path, scan_id, table)
        flash(
            request,
            f"Scanning {filename}: {table.rows:,} records, {len(table.columns)} fields. "
            "The file is read in memory and not stored.",
        )
        return redirect(f"/engagements/{eid}/discovery?scan={scan_id}")

    @app.get("/engagements/{eid}/discovery/scans/{sid}.json")
    def discovery_scan_status(eid: int, sid: int, user: User, conn: Conn):
        row = conn.execute(
            "SELECT id, status, progress, personal_fields, error FROM scan_jobs "
            "WHERE id = ? AND engagement_id = ?",
            (sid, eid),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Scan not found")
        return JSONResponse(dict(row), headers={"Cache-Control": "no-store"})

    @app.post("/engagements/{eid}/discovery/findings/{fid}")
    async def discovery_review(eid: int, fid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        f = get_finding(conn, eid, fid)
        action = str(form.get("action", ""))
        note = str(form.get("note", "")).strip()[:300]
        if action not in ("confirm", "reject", "reopen"):
            raise HTTPException(status_code=400, detail="Unknown action")
        if action == "reject" and not note and f["confidence"] != "low":
            flash(request, "Say why this isn't personal data, so the next reviewer knows.", "error")
            return redirect(f"/engagements/{eid}/discovery#f{fid}")
        status = {"confirm": "confirmed", "reject": "rejected", "reopen": "pending"}[action]
        conn.execute(
            "UPDATE scan_findings SET status = ?, note = ?, reviewed_by = ?, reviewed_at = ? "
            "WHERE id = ?",
            (
                status,
                note,
                user if action != "reopen" else None,
                db.now() if action != "reopen" else None,
                fid,
            ),
        )
        if action == "confirm":
            _add_to_inventory(conn, f, user)
        elif f["status"] == "confirmed":
            # Undoing a confirmation removes the record it created, unless someone has
            # already documented it (then it stays and must be deleted on purpose).
            conn.execute(
                "DELETE FROM data_inventory WHERE engagement_id = ? AND finding_id = ? AND "
                "purpose = '' AND legal_basis = '' AND retention = '' AND owner = ''",
                (eid, fid),
            )
        db.audit(
            conn,
            user,
            f"finding_{status}",
            eid,
            {"field": f["column_name"], "source": f["source_name"], "note": note},
        )
        label = f"{f['source_name']} · {f['column_name']}"
        flash(
            request,
            {
                "confirm": f"Added {label} to the data inventory.",
                "reject": f"Marked {label} as not personal data.",
                "reopen": f"{label} is back in the review queue.",
            }[action],
        )
        # Keep the reviewer's place: jump to the next finding still waiting in the same
        # order the page lists them.
        nxt = conn.execute(
            "SELECT id FROM scan_findings WHERE engagement_id = ? AND status = 'pending' "
            "ORDER BY source_name, CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id LIMIT 1",
            (eid,),
        ).fetchone()
        return redirect(f"/engagements/{eid}/discovery" + (f"#f{nxt[0]}" if nxt else ""))

    @app.post("/engagements/{eid}/discovery/confirm-high")
    async def discovery_confirm_high(eid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        open_engagement(conn, eid)
        rows = conn.execute(
            "SELECT * FROM scan_findings WHERE engagement_id = ? AND status = 'pending' "
            "AND confidence = 'high'",
            (eid,),
        ).fetchall()
        for f in rows:
            conn.execute(
                "UPDATE scan_findings SET status = 'confirmed', reviewed_by = ?, reviewed_at = ? "
                "WHERE id = ?",
                (user, db.now(), f["id"]),
            )
            _add_to_inventory(conn, f, user)
        if rows:
            db.audit(conn, user, "findings_confirmed", eid, {"count": len(rows)})
        flash(request, f"Confirmed {len(rows)} high-confidence finding(s).")
        return redirect(f"/engagements/{eid}/discovery")

    @app.get("/engagements/{eid}/discovery/findings.csv")
    def discovery_csv(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        rows = conn.execute(
            "SELECT * FROM scan_findings WHERE engagement_id = ? AND status != 'superseded' "
            "ORDER BY source_name, column_name",
            (eid,),
        ).fetchall()
        header = [
            "Source",
            "Field",
            "Looks like",
            "Category",
            "Risk",
            "Confidence",
            "Share of values matched",
            "Values sampled",
            "Under-18s seen",
            "Status",
            "Note",
        ]
        body = [
            [
                r["source_name"],
                r["column_name"],
                _finding_view(r)["label"],
                r["category"],
                r["risk"],
                r["confidence"],
                f"{r['match_ratio']:.0%}",
                r["sampled"],
                r["minors"],
                r["status"],
                r["note"],
            ]
            for r in rows
        ]
        return _csv(eng, "personal-data-findings", header, body)

    @app.get("/discovery/samples/{name}")
    def discovery_sample(name: str, user: User):
        if name not in SAMPLE_FILES:
            raise HTTPException(status_code=404, detail="No such sample")
        return Response(
            sample_bytes(name),
            media_type=SAMPLE_FILES[name][0],
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # ---- inventory

    @app.get("/engagements/{eid}/inventory")
    def inventory_page(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        register_ = request.app.state.register
        return render(
            request,
            "inventory.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="personal",
            subtab="inventory",
            ds=summary(conn, eid),
            items=inventory_rows(conn, eid),
            obligations={o.id: o for o in register_.obligations},
            kinds=catalog.KINDS,
            legal_bases=catalog.LEGAL_BASES,
            principals=catalog.PRINCIPALS,
            categories=sorted({k.category for k in catalog.KINDS.values()} | {"Children"}),
        )

    @app.post("/engagements/{eid}/inventory")
    async def inventory_add(eid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        source = str(form.get("source_name", "")).strip()[:80]
        field = str(form.get("field", "")).strip()[:120]
        kind = str(form.get("kind", ""))
        if not source or not field or kind not in catalog.KINDS:
            flash(request, "Give the system, the field and what kind of data it holds.", "error")
            return redirect(f"/engagements/{eid}/inventory")
        k = catalog.KINDS[kind]
        cur = conn.execute(
            "INSERT OR IGNORE INTO data_inventory (engagement_id, source_name, field, kind, "
            "category, risk, created_by, created_at, updated_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, source, field, kind, k.category, k.risk, user, db.now(), user, db.now()),
        )
        if not cur.rowcount:
            flash(request, f"{source} · {field} is already in the inventory.", "error")
            return redirect(f"/engagements/{eid}/inventory")
        db.audit(conn, user, "inventory_added", eid, {"source": source, "field": field})
        flash(request, f"Added {source} · {field}. Fill in its purpose, basis and retention.")
        return redirect(f"/engagements/{eid}/inventory#i{cur.lastrowid}")

    @app.post("/engagements/{eid}/inventory/{iid}")
    async def inventory_edit(eid: int, iid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        item = get_item(conn, eid, iid)
        values = {k: str(form.get(k, "")).strip()[: LIMITS.get(k, 80)] for k in EDITABLE}
        if values["legal_basis"] not in catalog.LEGAL_BASES:
            raise HTTPException(status_code=400, detail="Unknown legal basis")
        if values["principals"] and values["principals"] not in catalog.PRINCIPALS:
            raise HTTPException(status_code=400, detail="Unknown data principal category")
        children = 1 if item["children"] or values["principals"] == "Children" else 0
        conn.execute(
            # Safe: the SQL text holds only names from this code; values are ? parameters.
            "UPDATE data_inventory SET "  # nosec B608  # noqa: S608
            + ", ".join(f"{k} = ?" for k in EDITABLE)
            + ", children = ?, updated_by = ?, updated_at = ? WHERE id = ?",
            (*values.values(), children, user, db.now(), iid),
        )
        gaps = [label for key, label in REQUIRED.items() if not values[key]]
        db.audit(
            conn,
            user,
            "inventory_updated",
            eid,
            {"source": item["source_name"], "field": item["field"], "gaps": len(gaps)},
        )
        label = f"{item['source_name']} · {item['field']}"
        flash(request, f"Saved {label}." + (f" Still missing: {', '.join(gaps)}." if gaps else ""))
        return redirect(f"/engagements/{eid}/inventory#i{iid}")

    @app.post("/engagements/{eid}/inventory/{iid}/delete")
    async def inventory_delete(eid: int, iid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        open_engagement(conn, eid)
        item = get_item(conn, eid, iid)
        conn.execute("DELETE FROM data_inventory WHERE id = ?", (iid,))
        if item["finding_id"]:
            conn.execute(
                "UPDATE scan_findings SET status = 'pending', reviewed_by = NULL, "
                "reviewed_at = NULL WHERE id = ? AND status = 'confirmed'",
                (item["finding_id"],),
            )
        db.audit(
            conn,
            user,
            "inventory_removed",
            eid,
            {"source": item["source_name"], "field": item["field"]},
        )
        flash(request, f"Removed {item['source_name']} · {item['field']} from the inventory.")
        return redirect(f"/engagements/{eid}/inventory")

    @app.get("/engagements/{eid}/inventory.csv")
    def inventory_csv(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        # Same references as the page: "Section 5(1)", not the register's internal IDs.
        sources = {o.id: o.source for o in request.app.state.register.obligations}
        header = [
            "Source",
            "Field",
            "Data",
            "Category",
            "Risk",
            "Children",
            "Whose data",
            "Purpose",
            "Legal basis",
            "Retention",
            "Stored in",
            "Shared with",
            "Owner",
            "Obligations",
            "Missing",
        ]
        body = [
            [
                i["source_name"],
                i["field"],
                i["label"],
                i["category"],
                i["risk"],
                "yes" if i["children"] else "",
                i["principals"],
                i["purpose"],
                catalog.LEGAL_BASES.get(i["legal_basis"], i["legal_basis"]),
                i["retention"],
                i["storage_location"],
                i["recipients"],
                i["owner"],
                "; ".join(sources.get(o, o) for o in i["obligations"]),
                "; ".join(i["gaps"]),
            ]
            for i in inventory_rows(conn, eid)
        ]
        return _csv(eng, "data-inventory", header, body)


def _csv(eng, name: str, header: list[str], body: list[list]) -> Response:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for row in body:
        w.writerow([safe_cell(c) for c in row])
    slug = "".join(ch if ch.isalnum() else "-" for ch in eng["client"]).strip("-").lower()[:40]
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}-{slug}.csv"'},
    )
