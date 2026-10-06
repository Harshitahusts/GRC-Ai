"""Compliance controls, the evidence library, the audit log, the work queue and the team.

- Controls: the client's own status for every obligation in the DPDPA register, with an
  owner, notes and a review date. "Not applicable" needs a reason and an admin.
- Evidence: uploaded files linked to an obligation or a register record. Files are
  checked (type, size, content signature), stored outside the web root under a random
  name, and only downloadable by signed-in users. When an AI provider is set up, the AI
  reads each file linked to an obligation and says whether it's about that obligation;
  a file it flags stops counting as evidence until a person overrules it.
- Audit log: every recorded action, filterable, hash-chained so tampering shows.
- Work queue: open tasks, requests, breaches and reviews across all clients.
- Team: accounts and roles (admin, member, viewer).
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

import anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from grc_agent import evidence_check, plan
from grc_agent.discovery import engine as discovery_engine
from grc_agent.discovery import scanner
from grc_agent.web import (
    ai_views,
    auth_views,
    datamanager,
    db,
    discovery_views,
    mailer,
    register_views,
)
from grc_agent.web.registers import REGISTERS, TASKS
from grc_agent.web.security import hash_password

CONTROL_STATUSES = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "needs_review": "Needs review",
    "implemented": "Implemented",
    "not_applicable": "Not applicable",
}
CONTROL_BADGE = {
    "not_started": "badge-none",
    "in_progress": "badge-warn",
    "needs_review": "badge-warn",
    "implemented": "badge-pass",
    "not_applicable": "badge-none",
}
ROLES = {
    "admin": "Admin: everything, including the team",
    "member": "Member: all client work",
    "viewer": "Viewer: read-only",
}

EVIDENCE_MAX_BYTES = int(float(os.getenv("GRC_EVIDENCE_MAX_MB", "10")) * 1024 * 1024)
EVIDENCE_CATEGORIES = (
    "Policy or procedure",
    "Screenshot",
    "Contract or DPA",
    "Training record",
    "Log or system export",
    "Report or assessment",
    "Notice or consent form",
    "Other",
)
# Allowed file types, and how their first bytes must look (None: plain text).
EVIDENCE_TYPES = {
    ".pdf": ("application/pdf", b"%PDF"),
    ".png": ("image/png", b"\x89PNG"),
    ".jpg": ("image/jpeg", b"\xff\xd8"),
    ".jpeg": ("image/jpeg", b"\xff\xd8"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK"),
    ".csv": ("text/csv", None),
    ".txt": ("text/plain", None),
    ".md": ("text/markdown", None),
    ".json": ("application/json", None),
}


# A file the AI flagged as off-topic counts again once a person overrules the flag.
COUNTS_AS_EVIDENCE = (
    "(ai_check NOT IN ('not_relevant', 'too_little_content') OR check_overruled_by != '')"
)


class EvidenceError(ValueError):
    pass


def check_upload(filename: str, data: bytes) -> tuple[str, str]:
    """(extension, content type) for an acceptable evidence file, or EvidenceError."""
    ext = Path(filename).suffix.lower()
    if ext not in EVIDENCE_TYPES:
        raise EvidenceError(
            "Upload a PDF, image, Word, Excel, CSV, text or JSON file "
            f"({', '.join(sorted(EVIDENCE_TYPES))})."
        )
    if not data:
        raise EvidenceError("The file is empty.")
    if len(data) > EVIDENCE_MAX_BYTES:
        raise EvidenceError(f"The file is over {EVIDENCE_MAX_BYTES // (1024 * 1024)} MB.")
    ctype, magic = EVIDENCE_TYPES[ext]
    if magic is not None and not data.startswith(magic):
        raise EvidenceError(f"The file's contents don't match its {ext} extension.")
    if magic is None:
        try:
            data[:4096].decode("utf-8")
        except UnicodeDecodeError:
            raise EvidenceError("A text file must be UTF-8 text.") from None
    return ext, ctype


def evidence_dir(app) -> Path:
    d = Path(app.state.data_dir) / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def controls_for(conn: sqlite3.Connection, app, eid: int) -> list[dict]:
    """Every obligation with the client's control status, evidence and open tasks."""
    saved = {
        r["obligation_id"]: dict(r)
        for r in conn.execute("SELECT * FROM controls WHERE engagement_id = ?", (eid,))
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
    tasks: dict[str, int] = {}
    for r in conn.execute(
        "SELECT obligation_id, status FROM records WHERE engagement_id = ? AND register = 'tasks'",
        (eid,),
    ):
        if r["status"] in TASKS.open_statuses:
            tasks[r["obligation_id"]] = tasks.get(r["obligation_id"], 0) + 1
    findings = {
        r["obligation_id"]: r["status"]
        for r in conn.execute(
            "SELECT obligation_id, status FROM findings WHERE engagement_id = ?", (eid,)
        )
    }
    connector = discovery_views_connector_counts(conn, app, eid)
    out = []
    for o in app.state.register.obligations:
        c = saved.get(o.id, {})
        status = c.get("status", "not_started")
        out.append(
            {
                "o": o,
                "status": status,
                "label": CONTROL_STATUSES[status],
                "badge": CONTROL_BADGE[status],
                "owner": c.get("owner", ""),
                "notes": c.get("notes", ""),
                "na_reason": c.get("na_reason", ""),
                "reviewed_by": c.get("reviewed_by", ""),
                "review_date": c.get("review_date", ""),
                "updated_by": c.get("updated_by", ""),
                "files": files.get(o.id, 0),
                "checks": connector.get(o.source, 0),
                "open_tasks": tasks.get(o.id, 0),
                "assessment": findings.get(o.id),
            }
        )
    return out


def discovery_views_connector_counts(conn, app, eid: int) -> dict[str, int]:
    """Passing connector checks per provision, e.g. {"Section 8(5)": 3}."""
    out: dict[str, int] = {}
    for r in conn.execute(
        "SELECT provisions_json, status FROM evidence WHERE engagement_id = ?", (eid,)
    ):
        if r["status"] != "pass":
            continue
        for p in json.loads(r["provisions_json"] or "[]"):
            out[p] = out.get(p, 0) + 1
    return out


def controls_summary(items: list[dict]) -> dict:
    applicable = [c for c in items if c["status"] != "not_applicable"]
    done = sum(c["status"] == "implemented" for c in applicable)
    return {
        "applicable": len(applicable),
        "implemented": done,
        "in_progress": sum(c["status"] in ("in_progress", "needs_review") for c in applicable),
        "not_started": sum(c["status"] == "not_started" for c in applicable),
        "na": len(items) - len(applicable),
        "pct": round(100 * done / len(applicable)) if applicable else 0,
    }


def readiness_plan(conn: sqlite3.Connection, app, eng) -> list[plan.Step]:
    """The engagement's DPDPA readiness plan, worked out from its current state."""
    from grc_agent.web.app import delivery_checks

    eid = eng["id"]
    flagged = {
        r["obligation_id"]: r["n"]
        for r in conn.execute(
            # Safe: the SQL text holds only names from this code; values are ? parameters.
            "SELECT obligation_id, COUNT(*) AS n FROM evidence_files WHERE engagement_id = ? "  # nosec B608  # noqa: S608
            f"AND status = 'current' AND obligation_id != '' AND NOT {COUNTS_AS_EVIDENCE} "
            "GROUP BY obligation_id",
            (eid,),
        )
    }
    inventory = conn.execute(
        "SELECT COUNT(*) FROM data_inventory WHERE engagement_id = ?", (eid,)
    ).fetchone()[0]
    base = f"/engagements/{eid}"
    scope = [
        ("Intake submitted", bool(eng["intake_submitted_at"]), f"{base}/intake"),
        (
            "Assessment run on the current answers",
            bool(eng["assessed_at"]) and not eng["stale"],
            f"{base}/findings",
        ),
        (
            f"Personal data inventory started ({inventory} fields so far)",
            inventory > 0,
            f"{base}/discovery",
        ),
    ]
    delivery = [*delivery_checks(conn, eng), ("Delivered to the client", bool(eng["delivered_at"]))]
    return plan.build(eid, controls_for(conn, app, eid), flagged, scope, delivery)


# ---------------------------------------------------------------- AI relevance check


class CheckSkipped(Exception):
    """The file can't be checked; the message says why, in plain words."""


def ai_ready(app) -> bool:
    """An AI provider with a key is set up (the offline demo stand-in doesn't count)."""
    settings = app.state.ai
    if settings.demo:
        return False
    return app.state.ai_client is not None or ai_views.key_status_of(settings.provider)


def run_check(app, row) -> evidence_check.CheckResult:
    """Read the stored file and ask the AI. No database writes (safe in a thread)."""
    obligation = next(
        (o for o in app.state.register.obligations if o.id == row["obligation_id"]), None
    )
    if obligation is None:
        raise CheckSkipped("link it to an obligation first.")
    path = evidence_dir(app) / row["stored_name"]
    if not path.is_file():
        raise CheckSkipped("the file is missing from the server.")
    try:
        text = evidence_check.extract_text(path.read_bytes(), Path(row["stored_name"]).suffix)
    except evidence_check.Unreadable as exc:
        # Recorded on the file (no AI involved), so the row says why it wasn't checked.
        reason = str(exc)
        return evidence_check.CheckResult("unreadable", reason[:1].upper() + reason[1:] + ".", ())
    if app.state.ai.demo:
        raise CheckSkipped("the AI is in demo mode. Set up a provider under AI provider.")
    settings, client = ai_views.client_for(app)
    try:
        return evidence_check.check(
            client, settings, obligation, row["title"], row["category"], text
        )
    except anthropic.CredentialsError as exc:
        raise CheckSkipped(str(exc)) from None
    except TypeError as exc:  # the Anthropic client with no key at all
        if "authentication method" not in str(exc):
            raise
        raise CheckSkipped("no AI provider is set up. Add one under AI provider.") from None
    except anthropic.APIError as exc:
        raise CheckSkipped(
            f"the AI provider returned an error ({exc.__class__.__name__})."
        ) from None
    except ValueError:
        raise CheckSkipped("the AI's answer couldn't be read. Try again.") from None


def save_check(
    conn, row, result: evidence_check.CheckResult, settings, actor: str = "GRC Flow AI"
) -> None:
    conn.execute(
        "UPDATE evidence_files SET ai_check = ?, ai_check_reason = ?, ai_check_missing = ?, "
        "ai_checked_by = ?, ai_checked_at = ?, check_overruled_by = '' WHERE id = ?",
        (
            result.verdict,
            result.reason,
            json.dumps(list(result.missing)),
            "GRC Flow (no AI call)"
            if result.verdict == "unreadable"
            else f"{settings.provider_label} · {settings.model or 'auto'}",
            db.now(),
            row["id"],
        ),
    )
    db.audit(
        conn,
        actor,
        "evidence_checked",
        row["engagement_id"],
        {"title": row["title"], "verdict": result.verdict, "obligation": row["obligation_id"]},
    )


def check_in_background(app, fid: int) -> None:
    """After an upload: check the file quietly; a missing key just leaves it unchecked."""
    with db.connect(app.state.db_path) as conn:
        row = conn.execute("SELECT * FROM evidence_files WHERE id = ?", (fid,)).fetchone()
    if row is None or row["ai_check"]:
        return
    try:
        result = run_check(app, row)
    except CheckSkipped:
        return
    with db.connect(app.state.db_path) as conn:
        if conn.execute("SELECT 1 FROM evidence_files WHERE id = ?", (fid,)).fetchone():
            save_check(conn, row, result, app.state.ai)


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
        user_role,
    )

    def agent_engagement(conn, eid):
        return get_engagement(conn, eid)

    def open_engagement(conn, eid):
        # Delivery locks the assessment and its documents, not the day-to-day records:
        # breaches, requests and the rest keep their legal clocks after delivery.
        return agent_engagement(conn, eid)

    def people(conn) -> list[str]:
        return [r[0] for r in conn.execute("SELECT username FROM users ORDER BY username")]

    # ---- controls

    @app.get("/engagements/{eid}/controls")
    def controls_page(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        items = controls_for(conn, request.app, eid)
        return render(
            request,
            "controls.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="controls",
            items=items,
            cs=controls_summary(items),
            statuses=CONTROL_STATUSES,
            people=people(conn),
            is_admin=user_role(request) == "admin",
        )

    @app.post("/engagements/{eid}/controls/{oid}")
    async def control_save(eid: int, oid: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        ob = next((o for o in request.app.state.register.obligations if o.id == oid), None)
        if ob is None:
            raise HTTPException(status_code=404, detail="Unknown obligation")
        status = str(form.get("status", ""))
        if status not in CONTROL_STATUSES:
            raise HTTPException(status_code=400, detail="Unknown status")
        owner = str(form.get("owner", "")).strip()[:80]
        notes = str(form.get("notes", "")).strip()[:1000]
        reason = str(form.get("na_reason", "")).strip()[:500]
        review_date = str(form.get("review_date", "")).strip()[:10]
        if review_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_date):
            flash(request, "Use a date for the review date.", "error")
            return redirect(f"/engagements/{eid}/controls#{oid}")
        reviewed_by = ""
        if status == "not_applicable":
            if not reason:
                flash(request, "Say why this obligation doesn't apply.", "error")
                return redirect(f"/engagements/{eid}/controls#{oid}")
            if user_role(request) != "admin":
                flash(request, "Only an admin can mark an obligation not applicable.", "error")
                return redirect(f"/engagements/{eid}/controls#{oid}")
            reviewed_by = user
        if status == "implemented":
            has_evidence = conn.execute(
                # Safe: the SQL text holds only names from this code; values are ? parameters.
                "SELECT 1 FROM evidence_files WHERE engagement_id = ? AND obligation_id = ? "  # nosec B608  # noqa: S608
                f"AND status = 'current' AND {COUNTS_AS_EVIDENCE}",
                (eid, oid),
            ).fetchone() or discovery_views_connector_counts(conn, request.app, eid).get(ob.source)
            if not has_evidence and not notes:
                flash(
                    request,
                    "To mark it implemented, attach evidence or describe how it's done.",
                    "error",
                )
                return redirect(f"/engagements/{eid}/controls#{oid}")
        conn.execute(
            "INSERT INTO controls (engagement_id, obligation_id, status, owner, notes, na_reason, "
            "reviewed_by, review_date, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (engagement_id, obligation_id) DO UPDATE SET status = excluded.status, "
            "owner = excluded.owner, notes = excluded.notes, na_reason = excluded.na_reason, "
            "reviewed_by = excluded.reviewed_by, review_date = excluded.review_date, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (
                eid,
                oid,
                status,
                owner,
                notes,
                reason if status == "not_applicable" else "",
                reviewed_by,
                review_date,
                user,
                db.now(),
            ),
        )
        db.audit(
            conn,
            user,
            "control_updated",
            eid,
            {
                "obligation": oid,
                "source": ob.source,
                "status": status,
                "label": CONTROL_STATUSES[status],
            },
        )
        flash(request, f"{ob.source}: {CONTROL_STATUSES[status]}.")
        return redirect(f"/engagements/{eid}/controls#{oid}")

    # ---- readiness plan

    @app.get("/engagements/{eid}/plan")
    def plan_page(eid: int, request: Request, user: User, conn: Conn):
        eng = agent_engagement(conn, eid)
        steps = readiness_plan(conn, request.app, eng)
        return render(
            request,
            "plan.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="plan",
            steps=steps,
            current=plan.recommended(steps),
            done=sum(st.done for st in steps),
        )

    # ---- evidence

    @app.get("/engagements/{eid}/evidence")
    def evidence_page(
        eid: int, request: Request, user: User, conn: Conn, obligation: str = "", show: str = ""
    ):
        eng = agent_engagement(conn, eid)
        rows = conn.execute(
            "SELECT f.*, r.ref AS record_ref, r.register AS record_register "
            "FROM evidence_files f LEFT JOIN records r ON r.id = f.record_id "
            "WHERE f.engagement_id = ? ORDER BY f.id DESC",
            (eid,),
        ).fetchall()
        items = [dict(r) for r in rows if show == "all" or r["status"] == "current"]
        for i in items:
            i["missing"] = json.loads(i.get("ai_check_missing") or "[]")
        if obligation:
            items = [i for i in items if i["obligation_id"] == obligation]
        return render(
            request,
            "evidence.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="evidence",
            items=items,
            obligation=obligation,
            show=show,
            categories=EVIDENCE_CATEGORIES,
            types=", ".join(sorted(EVIDENCE_TYPES)),
            max_mb=EVIDENCE_MAX_BYTES // (1024 * 1024),
            obligations=request.app.state.register.obligations,
            human_size=datamanager.human_size,
            check_labels=evidence_check.VERDICTS,
            readable=evidence_check.READABLE,
            rejected=evidence_check.REJECTED,
            ai_ready=ai_ready(request.app),
        )

    def personal_data_in(filename: str, data: bytes) -> list[str]:
        """What personal data a CSV or JSON evidence file holds, found with the built-in
        scanner, so the uploader can be told to use a masked sample instead."""
        try:
            table = scanner.read_table(filename, data)
        except scanner.ScanInputError:
            return []
        found = scanner.scan_table(table, discovery_engine.BUILTIN)
        return list(dict.fromkeys(r.label for r in found if r.confidence != "low"))

    @app.post("/engagements/{eid}/evidence")
    async def evidence_upload(
        eid: int, request: Request, user: User, conn: Conn, background: BackgroundTasks
    ):
        form = await form_with_csrf(request)
        open_engagement(conn, eid)
        back = str(form.get("back", "")) or f"/engagements/{eid}/evidence"
        if not back.startswith(f"/engagements/{eid}/"):
            back = f"/engagements/{eid}/evidence"
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            flash(request, "Choose a file to upload.", "error")
            return redirect(back)
        data = await upload.read(EVIDENCE_MAX_BYTES + 1)
        await upload.close()
        try:
            ext, ctype = check_upload(upload.filename, data)
        except EvidenceError as e:
            flash(request, f"Couldn't upload {Path(upload.filename).name}: {e}", "error")
            return redirect(back)
        title = str(form.get("title", "")).strip()[:160] or Path(upload.filename).stem
        category = str(form.get("category", "Other"))
        category = category if category in EVIDENCE_CATEGORIES else "Other"
        ids = {o.id for o in request.app.state.register.obligations}
        obligation_id = str(form.get("obligation_id", ""))
        obligation_id = obligation_id if obligation_id in ids else ""
        record_id = str(form.get("record_id", ""))
        record = None
        if record_id.isdigit():
            record = conn.execute(
                "SELECT * FROM records WHERE id = ? AND engagement_id = ?", (int(record_id), eid)
            ).fetchone()
        if record and not obligation_id:
            obligation_id = record["obligation_id"]
        review_date = str(form.get("review_date", "")).strip()[:10]
        if review_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_date):
            review_date = ""
        version, replaces = 1, None
        replaces_id = str(form.get("replaces_id", ""))
        if replaces_id.isdigit():
            old = conn.execute(
                "SELECT * FROM evidence_files WHERE id = ? AND engagement_id = ?",
                (int(replaces_id), eid),
            ).fetchone()
            if old:
                version, replaces = old["version"] + 1, old["id"]
                # A new version keeps what the old one was linked to unless told otherwise.
                obligation_id = obligation_id or old["obligation_id"]
                if record is None and old["record_id"]:
                    record = conn.execute(
                        "SELECT * FROM records WHERE id = ? AND engagement_id = ?",
                        (old["record_id"], eid),
                    ).fetchone()
                if not str(form.get("category", "")):
                    category = old["category"]
                conn.execute(
                    "UPDATE evidence_files SET status = 'superseded' WHERE id = ?", (old["id"],)
                )
        stored = uuid.uuid4().hex + ext
        (evidence_dir(request.app) / stored).write_bytes(data)
        cur = conn.execute(
            "INSERT INTO evidence_files (engagement_id, title, category, obligation_id, "
            "record_id, filename, content_type, size_bytes, sha256, stored_name, version, "
            "replaces_id, review_date, uploaded_by, uploaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                title,
                category,
                obligation_id,
                record["id"] if record else None,
                Path(upload.filename).name[:160],
                ctype,
                len(data),
                hashlib.sha256(data).hexdigest(),
                stored,
                version,
                replaces,
                review_date,
                user,
                db.now(),
            ),
        )
        if record:
            register_views.add_event(
                conn, record["id"], eid, user, "comment", {"text": f"Attached evidence: {title}"}
            )
        db.audit(
            conn,
            user,
            "evidence_uploaded" if version == 1 else "evidence_replaced",
            eid,
            {"title": title, "version": version, "obligation": obligation_id},
        )
        auto = obligation_id and ai_ready(request.app)
        if auto:
            # The check runs after the response, on its own connection, so commit the
            # upload first (it must see the row, and not wait on this request's lock).
            conn.commit()
            background.add_task(check_in_background, request.app, cur.lastrowid)
        flash(
            request,
            f"Uploaded {title}"
            + (f" (version {version})." if version > 1 else ".")
            + (" The AI is checking that it's about the linked obligation." if auto else ""),
        )
        personal = personal_data_in(upload.filename, data) if ext in (".csv", ".json") else []
        if personal:
            flash(
                request,
                f"{title} holds personal data ({', '.join(personal[:5])}). Evidence only needs "
                "to show the control works: replace it with a masked sample or a summary "
                "where you can.",
                "warn",
            )
        return redirect(back)

    def get_file(conn, eid: int, fid: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM evidence_files WHERE id = ? AND engagement_id = ?", (fid, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Evidence not found")
        return row

    @app.get("/engagements/{eid}/evidence/{fid}/download")
    def evidence_download(eid: int, fid: int, request: Request, user: User, conn: Conn):
        agent_engagement(conn, eid)
        row = get_file(conn, eid, fid)
        path = evidence_dir(request.app) / row["stored_name"]
        if not path.is_file():
            raise HTTPException(status_code=410, detail="The file is missing from the server.")
        db.audit(conn, user, "evidence_downloaded", eid, {"title": row["title"]})
        return FileResponse(
            path,
            media_type=row["content_type"],
            filename=row["filename"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    @app.post("/engagements/{eid}/evidence/{fid}/check")
    async def evidence_check_now(eid: int, fid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        open_engagement(conn, eid)
        row = get_file(conn, eid, fid)
        try:
            result = await run_in_threadpool(run_check, request.app, row)
        except CheckSkipped as exc:
            flash(request, f"Couldn't check {row['title']}: {exc}", "error")
            return redirect(f"/engagements/{eid}/evidence#ev{fid}")
        save_check(conn, row, result, request.app.state.ai, user)
        if result.verdict == "unreadable":
            flash(request, f"Couldn't check {row['title']}: {result.reason}", "warn")
        else:
            flash(
                request,
                f"AI check of {row['title']}: {evidence_check.VERDICTS[result.verdict]}. "
                + result.reason,
                "warn" if result.verdict in evidence_check.REJECTED else "info",
            )
        return redirect(f"/engagements/{eid}/evidence#ev{fid}")

    @app.get("/engagements/{eid}/evidence/{fid}/text")
    def evidence_text(eid: int, fid: int, request: Request, user: User, conn: Conn):
        """The text GRC Flow reads from the file: what the AI check sees."""
        eng = agent_engagement(conn, eid)
        row = get_file(conn, eid, fid)
        path = evidence_dir(request.app) / row["stored_name"]
        if not path.is_file():
            raise HTTPException(status_code=410, detail="The file is missing from the server.")
        text, problem = "", ""
        try:
            text = evidence_check.extract_text(path.read_bytes(), Path(row["stored_name"]).suffix)
        except evidence_check.Unreadable as exc:
            problem = str(exc)
        db.audit(conn, user, "evidence_text_viewed", eid, {"title": row["title"]})
        limit = 50_000
        ob = next(
            (o for o in request.app.state.register.obligations if o.id == row["obligation_id"]),
            None,
        )
        return render(
            request,
            "evidence_text.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="evidence",
            f=row,
            ob=ob,
            text=text[:limit],
            cut=len(text) > limit,
            chars=len(text),
            ai_chars=evidence_check.MAX_CHARS,
            problem=problem,
            check_labels=evidence_check.VERDICTS,
            human_size=datamanager.human_size,
        )

    @app.post("/engagements/{eid}/evidence/{fid}/overrule")
    async def evidence_overrule(eid: int, fid: int, request: Request, user: User, conn: Conn):
        """A person decides the file counts as evidence after all."""
        await form_with_csrf(request)
        open_engagement(conn, eid)
        row = get_file(conn, eid, fid)
        conn.execute("UPDATE evidence_files SET check_overruled_by = ? WHERE id = ?", (user, fid))
        db.audit(
            conn,
            user,
            "evidence_check_overruled",
            eid,
            {"title": row["title"], "ai_check": row["ai_check"]},
        )
        flash(request, f"{row['title']} counts as evidence again (your decision is logged).")
        return redirect(f"/engagements/{eid}/evidence#ev{fid}")

    @app.post("/engagements/{eid}/evidence/{fid}/delete")
    async def evidence_delete(eid: int, fid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        open_engagement(conn, eid)
        row = get_file(conn, eid, fid)
        (evidence_dir(request.app) / row["stored_name"]).unlink(missing_ok=True)
        conn.execute("UPDATE evidence_files SET replaces_id = NULL WHERE replaces_id = ?", (fid,))
        conn.execute("DELETE FROM evidence_files WHERE id = ?", (fid,))
        db.audit(
            conn, user, "evidence_deleted", eid, {"title": row["title"], "sha256": row["sha256"]}
        )
        flash(request, f"Deleted {row['title']}.")
        return redirect(f"/engagements/{eid}/evidence")

    # ---- work queue, audit log

    @app.get("/work")
    def work_page(request: Request, user: User, conn: Conn, mine: str = ""):
        items = register_views.queue(conn, limit=200)
        if mine:
            items = [i for i in items if i["owner"].lower() == user.lower()]
        return render(
            request,
            "work.html",
            items=items,
            mine=bool(mine),
            personal=discovery_views.by_engagement(conn),
            registers=REGISTERS,
        )

    @app.get("/audit.csv")
    def audit_csv(request: Request, user: User, conn: Conn):
        """The whole log with its hashes, so an auditor can recompute the chain."""
        buf = io.StringIO()
        out = csv.writer(buf)
        cols = ("id", "at", "username", "engagement_id", "action", "detail", "prev_hash", "hash")
        out.writerow(cols)
        # Safe: the SQL text holds only names from this code; values are ? parameters.
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM audit_log ORDER BY id"):  # nosec B608  # noqa: S608
            out.writerow(["" if r[c] is None else r[c] for c in cols])
        db.audit(conn, user, "audit_log_exported")
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="grc-flow-audit-log.csv"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/audit")
    def audit_page(
        request: Request,
        user: User,
        conn: Conn,
        client: str = "",
        who: str = "",
        action: str = "",
        page: int = 1,
        verify: str = "",
    ):
        where, args = [], []
        if client.isdigit():
            where.append("a.engagement_id = ?")
            args.append(int(client))
        if who:
            where.append("a.username = ?")
            args.append(who)
        if action:
            where.append("a.action = ?")
            args.append(action)
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        per = 50
        page = max(page, 1)
        # Safe: the SQL text holds only names from this code; values are ? parameters.
        total = conn.execute(f"SELECT COUNT(*) FROM audit_log a {sql_where}", args).fetchone()[0]  # nosec B608  # noqa: S608
        rows = conn.execute(
            # Safe: the SQL text holds only names from this code; values are ? parameters.
            f"SELECT a.*, e.client FROM audit_log a LEFT JOIN engagements e "  # nosec B608  # noqa: S608
            f"ON e.id = a.engagement_id {sql_where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            [*args, per, (page - 1) * per],
        ).fetchall()
        chain = db.verify_audit_chain(conn) if verify else None
        return render(
            request,
            "audit.html",
            chain=chain,
            rows=rows,
            total=total,
            page=page,
            pages=max(1, -(-total // per)),
            client=client,
            who=who,
            action=action,
            clients=conn.execute("SELECT id, client FROM engagements ORDER BY client").fetchall(),
            users=people(conn),
            actions=[
                r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
            ],
        )

    # ---- team

    @app.get("/team")
    def team_page(request: Request, user: User, conn: Conn):
        rows = conn.execute(
            "SELECT u.username, u.role, u.created_at, u.email, "
            "u.password_hash LIKE '!%' AS no_password, "
            "(SELECT COUNT(*) FROM login_identities i "
            " WHERE LOWER(i.username) = LOWER(u.username)) AS sign_ins "
            "FROM users u ORDER BY u.username"
        ).fetchall()
        return render(
            request,
            "team.html",
            rows=rows,
            roles=ROLES,
            is_admin=user_role(request) == "admin",
            mail_on=mailer.configured(),
        )

    def require_admin(request):
        if user_role(request) != "admin":
            raise HTTPException(status_code=403, detail="Only an admin can manage the team.")

    @app.post("/team")
    async def team_add(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        require_admin(request)
        name = str(form.get("username", "")).strip()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        role = str(form.get("role", "member"))
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,40}", name):
            flash(request, "Use 2-40 letters, digits, dots, dashes or underscores.", "error")
            return redirect("/team")
        if role not in ROLES:
            raise HTTPException(status_code=400, detail="Unknown role")
        if conn.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (name,)).fetchone():
            flash(request, f"{name} already has an account.", "error")
            return redirect("/team")
        if problem := auth_views.email_problem(conn, email, name):
            flash(request, problem, "error")
            return redirect("/team")
        invite = not password
        if invite and not (email and mailer.configured()):
            flash(
                request,
                "Give a starting password, or an email address to send an invite to"
                + ("." if mailer.configured() else " (email isn't set up on this server yet)."),
                "error",
            )
            return redirect("/team")
        if password and len(password) < 10:
            flash(request, "Give a starting password of at least 10 characters.", "error")
            return redirect("/team")
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role, email) "
            "VALUES (?,?,?,?,?)",
            (
                name,
                auth_views.NO_PASSWORD if invite else hash_password(password),
                db.now(),
                role,
                email,
            ),
        )
        db.audit(conn, user, "user_created", None, {"username": name, "role": role})
        if invite:
            try:
                auth_views.send_invite(conn, request, name, email, user)
            except mailer.MailError as exc:
                flash(
                    request,
                    f"Added {name}, but the invite email didn't send: {exc} "
                    "Use “Send invite” to try again.",
                    "error",
                )
                return redirect("/team")
            flash(request, f"Added {name} as {role} and emailed an invite to {email}.")
        else:
            flash(request, f"Added {name} as {role}. Share the starting password privately.")
        return redirect("/team")

    @app.post("/team/{name}/email")
    async def team_email(name: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        require_admin(request)
        row = conn.execute(
            "SELECT username FROM users WHERE LOWER(username) = LOWER(?)", (name,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such user")
        email = str(form.get("email", "")).strip()
        if problem := auth_views.email_problem(conn, email, row["username"]):
            flash(request, problem, "error")
            return redirect("/team")
        conn.execute("UPDATE users SET email = ? WHERE LOWER(username) = LOWER(?)", (email, name))
        db.audit(
            conn, user, "email_changed", None, {"username": row["username"], "set": bool(email)}
        )
        flash(request, f"Saved the email for {row['username']}.")
        return redirect("/team")

    @app.post("/team/{name}/send-link")
    async def team_send_link(name: str, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        require_admin(request)
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (name,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such user")
        if not row["email"]:
            flash(request, f"Add an email for {row['username']} first.", "error")
            return redirect("/team")
        invite = not auth_views.has_password(row)
        try:
            if invite:
                auth_views.send_invite(conn, request, row["username"], row["email"], user)
            else:
                auth_views.send_reset(conn, request, row["username"], row["email"], user)
        except mailer.MailError as exc:
            flash(request, f"The email didn't send: {exc}", "error")
            return redirect("/team")
        flash(
            request,
            f"Emailed {'an invite' if invite else 'a password-reset link'} to {row['email']}.",
        )
        return redirect("/team")

    @app.post("/team/{name}/role")
    async def team_role(name: str, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        require_admin(request)
        role = str(form.get("role", ""))
        if role not in ROLES:
            raise HTTPException(status_code=400, detail="Unknown role")
        row = conn.execute(
            "SELECT role FROM users WHERE LOWER(username) = LOWER(?)", (name,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such user")
        admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        if row["role"] == "admin" and role != "admin" and admins <= 1:
            flash(request, "Keep at least one admin.", "error")
            return redirect("/team")
        conn.execute("UPDATE users SET role = ? WHERE LOWER(username) = LOWER(?)", (role, name))
        # The analyst's tools depend on the role, so it starts afresh with the new one.
        for key in [k for k in request.app.state.agents if k.lower() == name.lower()]:
            request.app.state.agents.pop(key, None)
        db.audit(conn, user, "role_changed", None, {"username": name, "role": role})
        flash(request, f"{name} is now {role}.")
        return redirect("/team")
