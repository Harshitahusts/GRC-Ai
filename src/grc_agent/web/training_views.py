"""DPDP training for a client's employees (in testing: on locally, off on a server).

Admins and managers import employees (CSV export from any HR system, or Zoho People),
add a video to each lesson (an uploaded file or a YouTube link) and share each person's
private training link. Employees need no account: the link opens their lessons. A lesson
can't be skipped (the server counts real watch time, at most 2x speed); after it, a quiz;
70% passes, otherwise the lesson is watched again. The Training page tracks who has
finished, by department, with a leaderboard.

Switch: GRC_TRAINING=1 turns it on, GRC_TRAINING=0 off; unset, it is on only when the app
isn't running on a domain (no GRC_DOMAIN), i.e. on a laptop or test machine.
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import csv
import hashlib
import io
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import UploadFile

from grc_agent import training
from grc_agent.connectors import hr
from grc_agent.connectors.base import ConnectorError
from grc_agent.web import access, db, https, mailer

VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
VIDEO_MAX_BYTES = int(float(os.getenv("GRC_TRAINING_VIDEO_MB", "300")) * 1024 * 1024)
YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
# The YouTube player needs YouTube's script and frame; only lesson pages get this policy.
YOUTUBE_CSP = (
    https.CSP.replace(
        "script-src 'self' 'unsafe-inline'",
        "script-src 'self' 'unsafe-inline' https://www.youtube.com https://s.ytimg.com",
    )
    + "; frame-src https://www.youtube-nocookie.com https://www.youtube.com"
)


def enabled() -> bool:
    flag = os.getenv("GRC_TRAINING", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return not os.getenv("GRC_DOMAIN", "").strip()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def youtube_id(url: str) -> str:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = YOUTUBE_ID.search(url)
    return m.group(1) if m else ""


def parse_duration(text: str) -> int:
    """'12:34', '1:02:03' or seconds -> seconds (0 if unreadable)."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds


def media_for(conn: sqlite3.Connection, eid: int) -> dict[str, dict]:
    return {
        r["lesson_id"]: dict(r)
        for r in conn.execute("SELECT * FROM training_media WHERE engagement_id = ?", (eid,))
    }


def duration_of(lesson: training.Lesson, media: dict | None) -> int:
    return media["duration_seconds"] if media else lesson.read_seconds


def progress_rows(conn: sqlite3.Connection, eid: int) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {}
    for r in conn.execute("SELECT * FROM training_progress WHERE engagement_id = ?", (eid,)):
        out.setdefault(r["employee_id"], {})[r["lesson_id"]] = dict(r)
    return out


def people(conn: sqlite3.Connection, eid: int) -> list[dict]:
    """Every active employee with how far they've got."""
    progress = progress_rows(conn, eid)
    total = len(training.COURSE)
    out = []
    for e in conn.execute(
        "SELECT * FROM employees WHERE engagement_id = ? AND active = 1 ORDER BY name", (eid,)
    ):
        mine = progress.get(e["id"], {})
        passed = [p for p in mine.values() if p["passed_at"]]
        scores = [p["best_score"] for p in passed if p["best_score"] is not None]
        out.append(
            {
                **dict(e),
                "passed": len(passed),
                "total": total,
                "done": len(passed) == total,
                "started": bool(mine),
                "avg": round(sum(scores) / len(scores)) if scores else None,
                "last": max((p["passed_at"] for p in passed), default=""),
                "attempts": sum(p["attempts"] for p in mine.values()),
                "has_link": bool(e["link_hash"]),
            }
        )
    return out


def leaderboard(rows: list[dict], limit: int = 10) -> list[dict]:
    """Most lessons passed, then best average score, then who got there first."""
    ranked = sorted(
        (r for r in rows if r["passed"]),
        key=lambda r: (-r["passed"], -(r["avg"] or 0), r["last"] or "9999"),
    )
    return [{**r, "rank": i + 1} for i, r in enumerate(ranked[:limit])]


def departments(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["department"] or "No department", []).append(r)
    out = []
    for name, members in groups.items():
        done = sum(m["done"] for m in members)
        scores = [m["avg"] for m in members if m["avg"] is not None]
        out.append(
            {
                "name": name,
                "people": len(members),
                "done": done,
                "pct": round(100 * done / len(members)),
                "avg": round(sum(scores) / len(scores)) if scores else None,
            }
        )
    return sorted(out, key=lambda d: (-d["pct"], d["name"]))


def published_policies(conn: sqlite3.Connection, eid: int) -> list[dict]:
    import json

    rows = conn.execute(
        "SELECT id, ref, title, data_json FROM records WHERE engagement_id = ? "
        "AND register = 'policies' AND status = 'published' ORDER BY id",
        (eid,),
    ).fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data_json"] or "{}")
        out.append(
            {
                "id": r["id"],
                "ref": r["ref"],
                "title": r["title"],
                "version": data.get("version", ""),
                "link": data.get("link", "") if data.get("link", "").startswith("https://") else "",
            }
        )
    return out


def upsert_people(
    conn: sqlite3.Connection, eid: int, found: list[hr.Person], source: str, by: str
) -> tuple[int, int]:
    """(added, updated). Someone removed earlier and imported again comes back."""
    added = updated = 0
    for p in found:
        row = conn.execute(
            "SELECT id FROM employees WHERE engagement_id = ? AND LOWER(email) = ?",
            (eid, p.email.lower()),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE employees SET name = ?, department = ?, manager = ?, external_id = ?, "
                "source = ?, active = 1 WHERE id = ?",
                (p.name, p.department, p.manager, p.external_id, source, row["id"]),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO employees (engagement_id, name, email, department, manager, source, "
                "external_id, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    p.name,
                    p.email.lower(),
                    p.department,
                    p.manager,
                    source,
                    p.external_id,
                    by,
                    db.now(),
                ),
            )
            added += 1
    return added, updated


def training_dir(app) -> Path:
    d = Path(app.state.data_dir) / "training"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    from grc_agent.web.auth_views import app_url

    def require_on() -> None:
        if not enabled():
            raise HTTPException(status_code=404, detail="Not found")

    def require_lead(conn, user: str, eid: int) -> None:
        if not access.leads(conn, user, eid):
            raise HTTPException(status_code=403, detail="Only an admin or manager can do this.")

    def employee(conn, eid: int, emp_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM employees WHERE id = ? AND engagement_id = ?", (emp_id, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Employee not found")
        return row

    def new_link(request: Request, conn, emp: sqlite3.Row) -> str:
        token = secrets.token_urlsafe(24)
        conn.execute("UPDATE employees SET link_hash = ? WHERE id = ?", (_hash(token), emp["id"]))
        return f"{app_url(request)}/learn/{token}"

    # ---------------------------------------------------------------- admin

    @app.get("/engagements/{eid}/training")
    def training_page(eid: int, request: Request, user: User, conn: Conn):
        require_on()
        eng = get_engagement(conn, eid)
        rows = people(conn, eid)
        media = media_for(conn, eid)
        hrc = conn.execute(
            "SELECT provider, status, message, synced_at FROM hr_connections "
            "WHERE engagement_id = ?",
            (eid,),
        ).fetchone()
        done = sum(r["done"] for r in rows)
        scores = [r["avg"] for r in rows if r["avg"] is not None]
        return render(
            request,
            "training.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="training",
            rows=rows,
            board=leaderboard(rows),
            depts=departments(rows),
            course=training.COURSE,
            media=media,
            stats={
                "people": len(rows),
                "done": done,
                "started": sum(r["started"] and not r["done"] for r in rows),
                "not_started": sum(not r["started"] for r in rows),
                "pct": round(100 * done / len(rows)) if rows else 0,
                "avg": round(sum(scores) / len(scores)) if scores else None,
            },
            hr_conn=hrc,
            zoho_dcs=hr.ZOHO_DCS,
            pass_mark=training.PASS_MARK,
            training_link=request.session.pop("training_link", None),
            can_lead=access.leads(conn, user, eid),
            mail_ready=mailer.configured(),
            max_video_mb=VIDEO_MAX_BYTES // (1024 * 1024),
        )

    @app.get("/engagements/{eid}/training.csv")
    def training_csv(eid: int, request: Request, user: User, conn: Conn):
        require_on()
        eng = get_engagement(conn, eid)
        progress = progress_rows(conn, eid)
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(
            ["Name", "Email", "Department", "Manager", "Lessons passed", "Average score"]
            + [f"{lesson.id} score" for lesson in training.COURSE]
        )
        from grc_agent.web.dataflow_views import safe_cell

        for r in people(conn, eid):
            mine = progress.get(r["id"], {})
            out.writerow(
                [safe_cell(v) for v in (r["name"], r["email"], r["department"], r["manager"])]
                + [r["passed"], "" if r["avg"] is None else r["avg"]]
                + [
                    (mine.get(lesson.id) or {}).get("best_score") or ""
                    for lesson in training.COURSE
                ]
            )
        db.audit(conn, user, "training_report_exported", eid)
        client = "".join(c if c.isalnum() else "-" for c in eng["client"]).strip("-")[:40]
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{client}-training.csv"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/engagements/{eid}/training/employees")
    async def employee_add(eid: int, request: Request, user: User, conn: Conn):
        require_on()
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        email = str(form.get("email", "")).strip().lower()
        name = str(form.get("name", "")).strip()[:120]
        if not hr.EMAIL.match(email) or not name:
            flash(request, "Enter a name and a valid email.", "error")
            return redirect(f"/engagements/{eid}/training#people")
        p = hr.Person(name, email, str(form.get("department", "")).strip()[:120])
        added, _ = upsert_people(conn, eid, [p], "manual", user)
        db.audit(conn, user, "training_employee_added", eid, {"email": email})
        flash(request, f"{name} {'added' if added else 'updated'}.")
        return redirect(f"/engagements/{eid}/training#people")

    @app.post("/engagements/{eid}/training/import")
    async def employee_import(eid: int, request: Request, user: User, conn: Conn):
        require_on()
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            flash(request, "Choose a CSV file exported from your HR system.", "error")
            return redirect(f"/engagements/{eid}/training#import")
        data = await upload.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024 or not upload.filename.lower().endswith(".csv"):
            flash(request, "Upload a .csv file under 5 MB.", "error")
            return redirect(f"/engagements/{eid}/training#import")
        try:
            found, skipped = hr.from_csv(data)
        except ConnectorError as exc:
            flash(request, f"Couldn't import: {exc}", "error")
            return redirect(f"/engagements/{eid}/training#import")
        added, updated = upsert_people(conn, eid, found, "csv", user)
        db.audit(
            conn,
            user,
            "training_employees_imported",
            eid,
            {"source": "csv", "added": added, "updated": updated, "skipped": len(skipped)},
        )
        msg = f"Imported: {added} added, {updated} updated."
        if skipped:
            msg += f" Skipped {len(skipped)}: " + "; ".join(skipped[:5])
        flash(request, msg, "warn" if skipped else "success")
        return redirect(f"/engagements/{eid}/training#people")

    def zoho_sync(conn, eid: int, user: str, settings: dict) -> str:
        try:
            found = hr.zoho_people(
                settings["dc"],
                settings["client_id"],
                settings["client_secret"],
                settings["refresh_token"],
            )
        except ConnectorError as exc:
            conn.execute(
                "UPDATE hr_connections SET status = 'error', message = ? WHERE engagement_id = ?",
                (str(exc)[:300], eid),
            )
            return f"Zoho People: {exc}"
        added, updated = upsert_people(conn, eid, found, "zoho", user)
        conn.execute(
            "UPDATE hr_connections SET status = 'ok', message = ?, synced_at = ? "
            "WHERE engagement_id = ?",
            (f"{len(found)} active employees", db.now(), eid),
        )
        db.audit(
            conn,
            user,
            "training_employees_imported",
            eid,
            {"source": "zoho", "added": added, "updated": updated},
        )
        return f"Zoho People: {added} added, {updated} updated."

    @app.post("/engagements/{eid}/training/zoho")
    async def zoho_connect(eid: int, request: Request, user: User, conn: Conn):
        require_on()
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        settings = {
            k: str(form.get(k, "")).strip()[:500]
            for k in ("dc", "client_id", "client_secret", "refresh_token")
        }
        if settings["dc"] not in hr.ZOHO_DCS or not all(settings.values()):
            flash(
                request,
                "Fill in the data centre, client ID, client secret and refresh token.",
                "error",
            )
            return redirect(f"/engagements/{eid}/training#import")
        sealed = request.app.state.secret_box.seal(settings)
        conn.execute(
            "INSERT INTO hr_connections (engagement_id, provider, settings_enc, updated_by, "
            "updated_at) VALUES (?,?,?,?,?) ON CONFLICT (engagement_id) DO UPDATE SET "
            "provider = excluded.provider, settings_enc = excluded.settings_enc, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (eid, "zoho_people", sealed, user, db.now()),
        )
        db.audit(conn, user, "hr_connection_saved", eid, {"provider": "zoho_people"})
        msg = zoho_sync(conn, eid, user, settings)
        flash(request, msg, "error" if "added" not in msg else "success")
        return redirect(f"/engagements/{eid}/training#import")

    @app.post("/engagements/{eid}/training/zoho/sync")
    async def zoho_sync_now(eid: int, request: Request, user: User, conn: Conn):
        require_on()
        await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        row = conn.execute(
            "SELECT settings_enc FROM hr_connections WHERE engagement_id = ?", (eid,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No HR connection")
        try:
            settings = request.app.state.secret_box.open(row["settings_enc"])
        except ValueError as exc:
            flash(request, str(exc), "error")
            return redirect(f"/engagements/{eid}/training#import")
        msg = zoho_sync(conn, eid, user, settings)
        flash(request, msg, "error" if "added" not in msg else "success")
        return redirect(f"/engagements/{eid}/training#import")

    @app.post("/engagements/{eid}/training/zoho/remove")
    async def zoho_remove(eid: int, request: Request, user: User, conn: Conn):
        require_on()
        await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        conn.execute("DELETE FROM hr_connections WHERE engagement_id = ?", (eid,))
        db.audit(conn, user, "hr_connection_removed", eid, {"provider": "zoho_people"})
        flash(request, "Zoho People disconnected. Imported employees stay.")
        return redirect(f"/engagements/{eid}/training#import")

    @app.post("/engagements/{eid}/training/employees/{emp_id}/link")
    async def employee_link(eid: int, emp_id: int, request: Request, user: User, conn: Conn):
        require_on()
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        emp = employee(conn, eid, emp_id)
        link = new_link(request, conn, emp)
        db.audit(conn, user, "training_link_created", eid, {"email": emp["email"]})
        if form.get("send") and mailer.configured():
            try:
                mailer.send(
                    emp["email"],
                    "Your DPDP data protection training",
                    [
                        f"Hello {emp['name']},",
                        "Your organisation has set up short training on India's Digital "
                        "Personal Data Protection Act. Each lesson ends with a short quiz; "
                        f"{training.PASS_MARK}% passes.",
                        "This link is personal to you. Please don't forward it.",
                    ],
                    link,
                    "Start training",
                )
            except mailer.MailError as exc:
                flash(request, f"Couldn't email the link: {exc}", "error")
            else:
                flash(request, f"Training link emailed to {emp['email']}.")
                return redirect(f"/engagements/{eid}/training#people")
        request.session["training_link"] = {"name": emp["name"], "link": link}
        return redirect(f"/engagements/{eid}/training#people")

    @app.post("/engagements/{eid}/training/links")
    async def employee_links_all(eid: int, request: Request, user: User, conn: Conn):
        """Email a link to everyone who hasn't been sent one yet."""
        require_on()
        await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        if not mailer.configured():
            flash(request, "Email isn't set up here; copy each person's link instead.", "error")
            return redirect(f"/engagements/{eid}/training#people")
        sent = failed = 0
        for emp in conn.execute(
            "SELECT * FROM employees WHERE engagement_id = ? AND active = 1 AND link_hash = ''",
            (eid,),
        ).fetchall():
            link = new_link(request, conn, emp)
            try:
                mailer.send(
                    emp["email"],
                    "Your DPDP data protection training",
                    [
                        f"Hello {emp['name']},",
                        "Your organisation has set up short training on India's Digital "
                        f"Personal Data Protection Act. {training.PASS_MARK}% in each quiz "
                        "passes.",
                        "This link is personal to you. Please don't forward it.",
                    ],
                    link,
                    "Start training",
                )
                sent += 1
            except mailer.MailError:
                conn.execute("UPDATE employees SET link_hash = '' WHERE id = ?", (emp["id"],))
                failed += 1
        db.audit(conn, user, "training_links_sent", eid, {"sent": sent, "failed": failed})
        flash(
            request,
            f"Emailed {sent} link{'s' if sent != 1 else ''}."
            + (f" {failed} failed." if failed else ""),
            "warn" if failed else "success",
        )
        return redirect(f"/engagements/{eid}/training#people")

    @app.post("/engagements/{eid}/training/employees/{emp_id}/remove")
    async def employee_remove(eid: int, emp_id: int, request: Request, user: User, conn: Conn):
        require_on()
        await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        emp = employee(conn, eid, emp_id)
        # Kept (inactive) so their training record stays as proof; the link stops working.
        conn.execute("UPDATE employees SET active = 0, link_hash = '' WHERE id = ?", (emp_id,))
        db.audit(conn, user, "training_employee_removed", eid, {"email": emp["email"]})
        flash(request, f"{emp['name']} removed. Their training record is kept.")
        return redirect(f"/engagements/{eid}/training#people")

    @app.post("/engagements/{eid}/training/media/{lesson_id}")
    async def media_save(eid: int, lesson_id: str, request: Request, user: User, conn: Conn):
        require_on()
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        lesson = training.BY_ID.get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Unknown lesson")
        back = f"/engagements/{eid}/training#lessons"
        duration = parse_duration(str(form.get("duration", "")))
        if not 10 <= duration <= 4 * 3600:
            flash(request, "Give the video's length, for example 6:30.", "error")
            return redirect(back)
        upload = form.get("file")
        if isinstance(upload, UploadFile) and upload.filename:
            ext = Path(upload.filename).suffix.lower()
            if ext not in VIDEO_TYPES:
                flash(request, "Upload an MP4 or WebM video.", "error")
                return redirect(back)
            data = await upload.read(VIDEO_MAX_BYTES + 1)
            if len(data) > VIDEO_MAX_BYTES:
                flash(request, f"Videos can be up to {VIDEO_MAX_BYTES // 1048576} MB.", "error")
                return redirect(back)
            sniff = data[4:8] == b"ftyp" if ext == ".mp4" else data[:4] == b"\x1aE\xdf\xa3"
            if not sniff:
                flash(request, "That file doesn't look like a video.", "error")
                return redirect(back)
            stored = uuid.uuid4().hex + ext
            (training_dir(request.app) / stored).write_bytes(data)
            kind, ref = "upload", stored
        else:
            ref = youtube_id(str(form.get("youtube", "")))
            if not ref:
                flash(request, "Upload a video file or paste a YouTube link.", "error")
                return redirect(back)
            kind = "youtube"
        old = media_for(conn, eid).get(lesson_id)
        conn.execute(
            "INSERT INTO training_media (engagement_id, lesson_id, kind, ref, duration_seconds, "
            "updated_by, updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (engagement_id, "
            "lesson_id) DO UPDATE SET kind = excluded.kind, ref = excluded.ref, "
            "duration_seconds = excluded.duration_seconds, updated_by = excluded.updated_by, "
            "updated_at = excluded.updated_at",
            (eid, lesson_id, kind, ref, duration, user, db.now()),
        )
        if old and old["kind"] == "upload" and old["ref"] != ref:
            (training_dir(request.app) / old["ref"]).unlink(missing_ok=True)
        db.audit(conn, user, "training_video_set", eid, {"lesson": lesson_id, "kind": kind})
        flash(request, f"Video set for “{lesson.title}”.")
        return redirect(back)

    @app.post("/engagements/{eid}/training/media/{lesson_id}/remove")
    async def media_remove(eid: int, lesson_id: str, request: Request, user: User, conn: Conn):
        require_on()
        await form_with_csrf(request)
        get_engagement(conn, eid)
        require_lead(conn, user, eid)
        old = media_for(conn, eid).get(lesson_id)
        if old:
            conn.execute(
                "DELETE FROM training_media WHERE engagement_id = ? AND lesson_id = ?",
                (eid, lesson_id),
            )
            if old["kind"] == "upload":
                (training_dir(request.app) / old["ref"]).unlink(missing_ok=True)
            db.audit(conn, user, "training_video_removed", eid, {"lesson": lesson_id})
        flash(request, "Video removed; the lesson is a reading again.")
        return redirect(f"/engagements/{eid}/training#lessons")

    @app.get("/engagements/{eid}/training/media/{lesson_id}/preview")
    def media_preview(eid: int, lesson_id: str, request: Request, user: User, conn: Conn):
        require_on()
        get_engagement(conn, eid)
        m = media_for(conn, eid).get(lesson_id)
        if not m or m["kind"] != "upload":
            raise HTTPException(status_code=404, detail="No video")
        return FileResponse(
            training_dir(request.app) / m["ref"], media_type=VIDEO_TYPES[Path(m["ref"]).suffix]
        )

    # ---------------------------------------------------------------- learner (no account)

    def learner(conn, token: str) -> sqlite3.Row:
        require_on()
        if not token or len(token) > 100:
            raise HTTPException(status_code=404, detail="Not found")
        row = conn.execute(
            "SELECT e.*, g.client FROM employees e JOIN engagements g ON g.id = e.engagement_id "
            "WHERE e.link_hash = ? AND e.active = 1",
            (_hash(token),),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="This training link isn't valid any more. Ask your admin for a new one.",
            )
        return row

    def lesson_state(conn, emp, lesson: training.Lesson, media: dict | None) -> dict:
        p = conn.execute(
            "SELECT * FROM training_progress WHERE employee_id = ? AND lesson_id = ?",
            (emp["id"], lesson.id),
        ).fetchone()
        duration = duration_of(lesson, media)
        watched = p["watched_seconds"] if p else 0
        return {
            "lesson": lesson,
            "duration": duration,
            "watched": min(watched, duration),
            "ready": training.watch_complete(watched, duration),
            "attempts": p["attempts"] if p else 0,
            "last_score": p["last_score"] if p else None,
            "best_score": p["best_score"] if p else None,
            "passed": bool(p and p["passed_at"]),
        }

    def learner_headers() -> dict[str, str]:
        # The link is the key: keep it out of caches and other sites' referrers.
        return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    @app.get("/learn/{token}")
    def learn_home(token: str, request: Request, conn: Conn):
        emp = learner(conn, token)
        media = media_for(conn, emp["engagement_id"])
        states = [
            lesson_state(conn, emp, lesson, media.get(lesson.id)) for lesson in training.COURSE
        ]
        accepted = {
            (r["record_id"], r["version"])
            for r in conn.execute(
                "SELECT record_id, version FROM training_acks WHERE employee_id = ?", (emp["id"],)
            )
        }
        policies = [
            {**p, "accepted": (p["id"], p["version"]) in accepted}
            for p in published_policies(conn, emp["engagement_id"])
        ]
        nxt = next((s for s in states if not s["passed"]), None)
        resp = render(
            request,
            "learn_home.html",
            emp=emp,
            token=token,
            states=states,
            next_lesson=nxt,
            passed=sum(s["passed"] for s in states),
            policies=policies,
            pass_mark=training.PASS_MARK,
        )
        resp.headers.update(learner_headers())
        return resp

    @app.get("/learn/{token}/lesson/{lesson_id}")
    def learn_lesson(token: str, lesson_id: str, request: Request, conn: Conn):
        emp = learner(conn, token)
        lesson = training.BY_ID.get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Unknown lesson")
        media = media_for(conn, emp["engagement_id"]).get(lesson_id)
        state = lesson_state(conn, emp, lesson, media)
        idx = training.COURSE.index(lesson)
        resp = render(
            request,
            "learn_lesson.html",
            emp=emp,
            token=token,
            st=state,
            lesson=lesson,
            media=media,
            speeds=training.SPEEDS,
            pass_mark=training.PASS_MARK,
            number=idx + 1,
            count=len(training.COURSE),
            result=request.session.pop("quiz_result", None),
        )
        resp.headers.update(learner_headers())
        if media and media["kind"] == "youtube":
            resp.headers["Content-Security-Policy"] = YOUTUBE_CSP
        return resp

    @app.get("/learn/{token}/lesson/{lesson_id}/video")
    def learn_video(token: str, lesson_id: str, request: Request, conn: Conn):
        emp = learner(conn, token)
        m = media_for(conn, emp["engagement_id"]).get(lesson_id)
        if not m or m["kind"] != "upload":
            raise HTTPException(status_code=404, detail="No video")
        path = training_dir(request.app) / m["ref"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No video")
        return FileResponse(
            path,
            media_type=VIDEO_TYPES[path.suffix],
            headers={"Cache-Control": "private, no-store"},
        )

    @app.post("/learn/{token}/lesson/{lesson_id}/beat")
    async def learn_beat(token: str, lesson_id: str, request: Request, conn: Conn):
        """The player reports where it is every few seconds; the server decides how much
        of that counts (no faster than the top speed allows in the real time that passed)."""
        emp = learner(conn, token)
        lesson = training.BY_ID.get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Unknown lesson")
        try:
            body = await request.json()
            position = float(body.get("position", 0))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=400, detail="Bad request") from None
        media = media_for(conn, emp["engagement_id"]).get(lesson_id)
        duration = duration_of(lesson, media)
        now = _now()
        p = conn.execute(
            "SELECT * FROM training_progress WHERE employee_id = ? AND lesson_id = ?",
            (emp["id"], lesson_id),
        ).fetchone()
        if p is None:
            conn.execute(
                "INSERT INTO training_progress (employee_id, engagement_id, lesson_id, "
                "duration_seconds, last_beat_at) VALUES (?,?,?,?,?)",
                (emp["id"], emp["engagement_id"], lesson_id, duration, now.isoformat()),
            )
            watched, elapsed = 0.0, 0.0
        else:
            watched = float(p["watched_seconds"])
            try:
                elapsed = (now - datetime.fromisoformat(p["last_beat_at"])).total_seconds()
            except ValueError:
                elapsed = 0.0
            # A long pause (tab closed) doesn't bank time: count at most one beat's worth.
            elapsed = min(elapsed, 15.0)
        watched = training.advance(watched, max(0.0, position), elapsed, duration)
        complete = training.watch_complete(watched, duration)
        conn.execute(
            "UPDATE training_progress SET watched_seconds = ?, duration_seconds = ?, "
            "last_beat_at = ?, watched_at = CASE WHEN ? AND watched_at = '' THEN ? "
            "ELSE watched_at END WHERE employee_id = ? AND lesson_id = ?",
            (
                int(watched),
                duration,
                now.isoformat(),
                complete,
                now.isoformat(),
                emp["id"],
                lesson_id,
            ),
        )
        return JSONResponse(
            {"watched": int(watched), "duration": duration, "complete": complete},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/learn/{token}/lesson/{lesson_id}/quiz")
    async def learn_quiz(token: str, lesson_id: str, request: Request, conn: Conn):
        emp = learner(conn, token)
        lesson = training.BY_ID.get(lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Unknown lesson")
        media = media_for(conn, emp["engagement_id"]).get(lesson_id)
        state = lesson_state(conn, emp, lesson, media)
        back = f"/learn/{token}/lesson/{lesson_id}"
        if state["passed"]:
            return redirect(back)
        if not state["ready"]:
            request.session["quiz_result"] = {"blocked": True}
            return redirect(back)
        form = await request.form()
        percent, wrong = training.score(lesson, {k: str(v) for k, v in form.items()})
        ok = training.passed(percent)
        now = db.now()
        conn.execute(
            "UPDATE training_progress SET attempts = attempts + 1, last_score = ?, "
            "best_score = CASE WHEN best_score IS NULL OR best_score < ? THEN ? ELSE best_score "
            "END, passed_at = ?, watched_seconds = ?, watched_at = ? "
            "WHERE employee_id = ? AND lesson_id = ?",
            (
                percent,
                percent,
                percent,
                now if ok else "",
                state["watched"] if ok else 0,  # failing means watching it again
                now if ok else "",
                emp["id"],
                lesson_id,
            ),
        )
        db.audit(
            conn,
            f"employee:{emp['email']}",
            "training_quiz_taken",
            emp["engagement_id"],
            {"lesson": lesson_id, "score": percent, "passed": ok},
        )
        request.session["quiz_result"] = {"score": percent, "passed": ok, "wrong": len(wrong)}
        return redirect(back)

    @app.post("/learn/{token}/policy/{record_id}")
    async def learn_ack(token: str, record_id: int, request: Request, conn: Conn):
        emp = learner(conn, token)
        policy = next(
            (p for p in published_policies(conn, emp["engagement_id"]) if p["id"] == record_id),
            None,
        )
        if policy is None:
            raise HTTPException(status_code=404, detail="Policy not found")
        conn.execute(
            "INSERT INTO training_acks (employee_id, engagement_id, record_id, version, "
            "accepted_at) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            (emp["id"], emp["engagement_id"], record_id, policy["version"], db.now()),
        )
        db.audit(
            conn,
            f"employee:{emp['email']}",
            "policy_accepted",
            emp["engagement_id"],
            {"policy": policy["ref"], "version": policy["version"]},
        )
        return redirect(f"/learn/{token}#policies")
