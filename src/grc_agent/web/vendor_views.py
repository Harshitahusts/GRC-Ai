"""Vendor due diligence by link: send a vendor a DPDP questionnaire, they answer without an
account, and the answers become a risk score on the vendor's record.

The link is a random token (only its hash is stored), valid for 30 days and for one
submission. Sending a new one cancels the old. Submitting updates the vendor record
(questionnaire received, risk, where data is processed) and adds the score to its history;
a person then reviews the answers and marks them reviewed.
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request

from grc_agent import vendor_questions as vq
from grc_agent.web import access, db, mailer
from grc_agent.web.register_views import add_event
from grc_agent.web.registers import REGISTERS

LINK_DAYS = 30
RISK_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def for_record(conn: sqlite3.Connection, rid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM vendor_questionnaires WHERE record_id = ? ORDER BY id DESC", (rid,)
    ).fetchall()
    return [{**dict(r), "expired": expired(r)} for r in rows]


def expired(row) -> bool:
    try:
        return datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc)
    except ValueError:
        return True


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

    def vendor(conn, eid: int, rid: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ? AND engagement_id = ? AND register = 'vendors'",
            (rid, eid),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Vendor not found")
        return row

    def set_vendor_fields(conn, rec: sqlite3.Row, changes: dict, user: str) -> None:
        data = json.loads(rec["data_json"] or "{}")
        changed = [k for k, v in changes.items() if v and data.get(k) != v]
        if not changed:
            return
        data.update({k: changes[k] for k in changed})
        conn.execute(
            "UPDATE records SET data_json = ?, updated_by = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data), user, db.now(), rec["id"]),
        )
        spec = REGISTERS["vendors"]
        add_event(
            conn,
            rec["id"],
            rec["engagement_id"],
            user,
            "edited",
            {"fields": [spec.field(k).label for k in changed]},
        )

    # ---------------------------------------------------------------- team side

    @app.post("/engagements/{eid}/r/vendors/{rid}/questionnaire")
    async def questionnaire_send(eid: int, rid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        rec = vendor(conn, eid, rid)
        back = f"/engagements/{eid}/r/vendors/{rid}#questionnaire"
        email = str(form.get("email", "")).strip().lower()[:200]
        if email and not mailer.valid_email(email):
            flash(request, "That email address doesn't look right.", "error")
            return redirect(back)
        conn.execute(
            "UPDATE vendor_questionnaires SET status = 'revoked' "
            "WHERE record_id = ? AND status = 'sent'",
            (rid,),
        )
        token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO vendor_questionnaires (engagement_id, record_id, token_hash, sent_to, "
            "sent_by, sent_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (
                eid,
                rid,
                _hash(token),
                email,
                user,
                now.isoformat(timespec="seconds"),
                (now + timedelta(days=LINK_DAYS)).isoformat(timespec="seconds"),
            ),
        )
        link = f"{app_url(request)}/vq/{token}"
        set_vendor_fields(conn, rec, {"questionnaire": "sent"}, user)
        add_event(
            conn,
            rid,
            eid,
            user,
            "comment",
            {"text": "DPDP questionnaire sent" + (f" to {email}" if email else "") + "."},
        )
        db.audit(conn, user, "vendor_questionnaire_sent", eid, {"vendor": rec["ref"]})
        if email and mailer.configured():
            eng = get_engagement(conn, eid)
            try:
                mailer.send(
                    email,
                    f"Data protection questionnaire from {eng['client']}",
                    [
                        "Hello,",
                        f"{eng['client']} asks its vendors that handle personal data to answer "
                        "a short questionnaire about how they protect it, under India's Digital "
                        "Personal Data Protection Act 2023. It takes about 10 minutes.",
                        f"The link works for {LINK_DAYS} days and can be submitted once.",
                    ],
                    link,
                    "Answer the questionnaire",
                )
            except mailer.MailError as exc:
                flash(request, f"Couldn't email it ({exc}). Share the link yourself.", "error")
            else:
                flash(request, f"Questionnaire emailed to {email}.")
                return redirect(back)
        request.session["vq_link"] = link
        return redirect(back)

    @app.get("/engagements/{eid}/vendor-questionnaires/{qid}")
    def questionnaire_view(eid: int, qid: int, request: Request, user: User, conn: Conn):
        eng = get_engagement(conn, eid)
        q = conn.execute(
            "SELECT * FROM vendor_questionnaires WHERE id = ? AND engagement_id = ?", (qid, eid)
        ).fetchone()
        if q is None:
            raise HTTPException(status_code=404, detail="Questionnaire not found")
        rec = vendor(conn, eid, q["record_id"])
        answers = json.loads(q["answers_json"] or "{}")
        result = (
            vq.score(answers.get("answers", {}), answers.get("certifications", []))
            if q["submitted_at"]
            else None
        )
        return render(
            request,
            "vendor_questionnaire.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="vendors",
            q=q,
            rec=rec,
            a=answers,
            result=result,
            questions=vq.QUESTIONS,
            answer_labels=dict(vq.ANSWERS),
            locations=dict(vq.LOCATIONS),
            certifications=dict(vq.CERTIFICATIONS),
            risk_labels=RISK_LABELS,
            can_review=access.leads(conn, user, eid),
        )

    @app.post("/engagements/{eid}/vendor-questionnaires/{qid}/review")
    async def questionnaire_review(eid: int, qid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        if not access.leads(conn, user, eid):
            raise HTTPException(status_code=403, detail="Only an admin or manager can review.")
        q = conn.execute(
            "SELECT * FROM vendor_questionnaires WHERE id = ? AND engagement_id = ? "
            "AND status = 'submitted'",
            (qid, eid),
        ).fetchone()
        if q is None:
            raise HTTPException(status_code=404, detail="Nothing to review")
        note = str(form.get("note", "")).strip()[:2000]
        risk = str(form.get("risk", ""))
        conn.execute(
            "UPDATE vendor_questionnaires SET status = 'reviewed', review_note = ?, "
            "reviewed_by = ?, reviewed_at = ?, risk = ? WHERE id = ?",
            (note, user, db.now(), risk if risk in RISK_LABELS else q["risk"], qid),
        )
        rec = vendor(conn, eid, q["record_id"])
        changes = {"questionnaire": "reviewed"}
        if risk in RISK_LABELS:
            changes["risk"] = risk
        set_vendor_fields(conn, rec, changes, user)
        add_event(
            conn,
            rec["id"],
            eid,
            user,
            "comment",
            {"text": "Questionnaire reviewed." + (f" {note}" if note else "")},
        )
        db.audit(conn, user, "vendor_questionnaire_reviewed", eid, {"vendor": rec["ref"]})
        flash(request, "Questionnaire marked reviewed.")
        return redirect(f"/engagements/{eid}/r/vendors/{rec['id']}#questionnaire")

    # ---------------------------------------------------------------- vendor side (no account)

    def open_link(conn, token: str) -> sqlite3.Row:
        if not token or len(token) > 100:
            raise HTTPException(status_code=404, detail="Not found")
        q = conn.execute(
            "SELECT q.*, e.client, r.title AS vendor_name FROM vendor_questionnaires q "
            "JOIN engagements e ON e.id = q.engagement_id JOIN records r ON r.id = q.record_id "
            "WHERE q.token_hash = ?",
            (_hash(token),),
        ).fetchone()
        if q is None or q["status"] == "revoked" or (q["status"] == "sent" and expired(q)):
            raise HTTPException(
                status_code=404,
                detail="This questionnaire link isn't valid any more. Ask for a new one.",
            )
        return q

    def headers(resp):
        resp.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return resp

    @app.get("/vq/{token}")
    def vendor_form(token: str, request: Request, conn: Conn):
        q = open_link(conn, token)
        return headers(
            render(
                request,
                "vq_form.html",
                q=q,
                token=token,
                sections=vq.SECTIONS,
                answers=vq.ANSWERS,
                locations=vq.LOCATIONS,
                certifications=vq.CERTIFICATIONS,
                done=q["status"] != "sent",
                errors=[],
                values={},
            )
        )

    @app.post("/vq/{token}")
    async def vendor_submit(token: str, request: Request, conn: Conn):
        q = open_link(conn, token)
        if q["status"] != "sent":
            return redirect(f"/vq/{token}")
        form = await request.form()
        allowed = {k for k, _ in vq.ANSWERS}
        answers = {qq.id: str(form.get(f"a_{qq.id}", "")) for qq in vq.QUESTIONS}
        errors = [qq.text for qq in vq.QUESTIONS if answers[qq.id] not in allowed]
        location = str(form.get("location", ""))
        certs = [c for c in form.getlist("certifications") if c in dict(vq.CERTIFICATIONS)]
        respondent = str(form.get("respondent", "")).strip()[:120]
        values = {
            "answers": answers,
            "comments": {
                qq.id: str(form.get(f"c_{qq.id}", "")).strip()[:1000] for qq in vq.QUESTIONS
            },
            "location": location if location in dict(vq.LOCATIONS) else "",
            "countries": str(form.get("countries", "")).strip()[:200],
            "subprocessors": str(form.get("subprocessors", "")).strip()[:2000],
            "certifications": certs,
            "respondent": respondent,
            "role": str(form.get("role", "")).strip()[:120],
        }
        if errors or not values["location"] or not respondent:
            return headers(
                render(
                    request,
                    "vq_form.html",
                    q=q,
                    token=token,
                    sections=vq.SECTIONS,
                    answers=vq.ANSWERS,
                    locations=vq.LOCATIONS,
                    certifications=vq.CERTIFICATIONS,
                    done=False,
                    errors=(["Answer every question."] if errors else [])
                    + ([] if values["location"] else ["Say where the data is processed."])
                    + ([] if respondent else ["Give your name."]),
                    values=values,
                    status_code=400,
                )
            )
        result = vq.score(answers, certs)
        now = db.now()
        conn.execute(
            "UPDATE vendor_questionnaires SET status = 'submitted', answers_json = ?, score = ?, "
            "risk = ?, respondent = ?, submitted_at = ? WHERE id = ? AND status = 'sent'",
            (json.dumps(values), result["pct"], result["risk"], respondent, now, q["id"]),
        )
        rec = conn.execute("SELECT * FROM records WHERE id = ?", (q["record_id"],)).fetchone()
        who = f"vendor:{respondent}"
        set_vendor_fields(
            conn,
            rec,
            {
                "questionnaire": "received",
                "risk": result["risk"],
                "location": values["location"],
                "countries": values["countries"],
            },
            who,
        )
        add_event(
            conn,
            rec["id"],
            q["engagement_id"],
            who,
            "comment",
            {
                "text": f"Questionnaire answered: {result['pct']}%, "
                f"{RISK_LABELS[result['risk']].lower()} risk"
                + (
                    f"; {len(result['critical'])} essential safeguard(s) missing."
                    if result["critical"]
                    else "."
                )
            },
        )
        db.audit(
            conn,
            who,
            "vendor_questionnaire_submitted",
            q["engagement_id"],
            {"vendor": rec["ref"], "score": result["pct"], "risk": result["risk"]},
        )
        return redirect(f"/vq/{token}")
