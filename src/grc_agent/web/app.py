"""FastAPI app. Server-rendered HTML, SQLite storage, session login.

Workflow per engagement (build plan, Step 6):
intake -> deterministic assessment -> documents -> human review -> delivery.
The hard stop (6.4): an engagement can't be delivered while any citation fails
to resolve, the assessment is stale, or any document is unreviewed.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from grc_agent.agent import Agent
from grc_agent.ai_assessment import AssessmentError, ClaudeAssessor
from grc_agent.assessment import assess, readiness_score
from grc_agent.config import Settings
from grc_agent.content import (
    TYPES as CONTENT_TYPES,
)
from grc_agent.content import (
    reading_minutes,
    render_markdown,
    seed_items,
    seo_checks,
    slugify,
    valid_slug,
)
from grc_agent.corpus import load_corpus
from grc_agent.documents import DOCUMENT_TYPES, Block, EngagementFacts, build_document, to_docx
from grc_agent.kpis import CorpusIndex, build_scorecard
from grc_agent.kpis.models import DOCUMENT_OUTCOMES, VERDICTS, parse_engagement
from grc_agent.register import CHOICES, corpus_index_path, load_register
from grc_agent.web import db
from grc_agent.web.security import (
    DUMMY_HASH,
    csrf_matches,
    new_csrf_token,
    verify_password,
)

HERE = Path(__file__).parent
SECTORS = ["EdTech", "BFSI", "Healthcare", "SaaS", "Retail", "Other"]
OUTCOME_LABELS = {
    "usable": "Usable as is (wording-only edits)",
    "minor_edits": "Minor edits (no material changes)",
    "material_edit": "Material edit (substance changed)",
    "full_rewrite": "Full rewrite (counts as fallback)",
}
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 300
SESSION_SECONDS = 8 * 3600


def _secret_key(data_dir: Path) -> str:
    if key := os.getenv("GRC_SECRET_KEY"):
        return key
    path = data_dir / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip()


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    data_dir = Path(data_dir or os.getenv("GRC_DATA_DIR", "var"))
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "grc.db"
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        db.seed_content(conn, seed_items())

    app = FastAPI(title="GRC agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = db_path
    app.state.login_failures = {}
    app.state.agents = {}
    app.state.demo = Settings.from_env().demo  # also loads .env
    app.state.register = load_register()
    app.state.corpus = load_corpus()
    if os.getenv("GRC_CORPUS_INDEX"):
        app.state.index = CorpusIndex.from_file(os.environ["GRC_CORPUS_INDEX"])
        app.state.index_source = "GRC_CORPUS_INDEX"
    elif app.state.corpus is not None:
        app.state.index = app.state.corpus.index
        app.state.index_source = "built corpus"
    else:
        app.state.index = CorpusIndex.from_file(corpus_index_path())
        app.state.index_source = "sample index"
    app.state.make_assessor = lambda corpus: ClaudeAssessor(corpus)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_secret_key(data_dir),
        session_cookie="grc_session",
        max_age=SESSION_SECONDS,
        same_site="strict",
        https_only=os.getenv("GRC_SECURE_COOKIES") == "1",
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    _routes(app)
    return app


templates = Jinja2Templates(directory=HERE / "templates")


# ---------------------------------------------------------------- helpers


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def form_with_csrf(request: Request) -> dict[str, Any]:
    form = await request.form()
    if not csrf_matches(request.session.get("csrf"), form.get("csrf")):
        raise HTTPException(status_code=403, detail="Form expired. Go back, reload and try again.")
    return {k: form.getlist(k) if len(form.getlist(k)) > 1 else form[k] for k in form}


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    if "csrf" not in request.session:
        request.session["csrf"] = new_csrf_token()
    context.update(
        request=request,
        user=request.session.get("user"),
        csrf=request.session["csrf"],
        flashes=request.session.pop("flash", []),
        register=request.app.state.register,
        demo_mode=request.app.state.demo,
        document_types=DOCUMENT_TYPES,
        choices=CHOICES,
        outcome_labels=OUTCOME_LABELS,
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def get_engagement(conn: sqlite3.Connection, engagement_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM engagements WHERE id = ?", (engagement_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Engagement not found")
    return row


def require_open_agent_engagement(eng: sqlite3.Row) -> None:
    if eng["mode"] != "agent":
        raise HTTPException(status_code=400, detail="Manual engagements have no agent workflow.")
    if eng["delivered_at"]:
        raise HTTPException(status_code=400, detail="This engagement is delivered and locked.")


def answers_of(conn: sqlite3.Connection, engagement_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT question_id, answer FROM intake_answers WHERE engagement_id = ?", (engagement_id,)
    )
    return {r["question_id"]: r["answer"] for r in rows}


def findings_of(conn: sqlite3.Connection, engagement_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM findings WHERE engagement_id = ? ORDER BY obligation_id", (engagement_id,)
    ).fetchall()


def documents_of(conn: sqlite3.Connection, engagement_id: int) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM documents WHERE engagement_id = ?", (engagement_id,))
    order = list(DOCUMENT_TYPES)
    return sorted(rows.fetchall(), key=lambda d: order.index(d["type"]))


def delivery_checks(conn: sqlite3.Connection, eng: sqlite3.Row) -> list[tuple[str, bool]]:
    """The hard stop. Every check must pass before delivery; there is no override."""
    if eng["mode"] == "manual":
        return [("Consultant hours recorded", eng["consultant_hours"] is not None)]
    findings = findings_of(conn, eng["id"])
    docs = documents_of(conn, eng["id"])
    return [
        ("Intake submitted", bool(eng["intake_submitted_at"])),
        (
            "Assessment run on the current intake answers",
            bool(eng["assessed_at"]) and not eng["stale"],
        ),
        (
            "Every finding's citation resolves",
            bool(findings) and all(f["citation_resolves"] for f in findings),
        ),
        ("All documents generated", {d["type"] for d in docs} == set(DOCUMENT_TYPES)),
        ("Every document reviewed by a person", bool(docs) and all(d["reviewed_at"] for d in docs)),
        # Demo-mode text is a placeholder, never AI output, so it can't go to a client.
        (
            "No demo-mode (placeholder) findings",
            not any(f["drafted_by"] == "demo" for f in findings),
        ),
    ]


def kpi_record(conn: sqlite3.Connection, eng: sqlite3.Row, register_size: int) -> dict[str, Any]:
    """Engagement in the grc-kpis JSON record format."""
    record: dict[str, Any] = {
        "id": f"ENG-{eng['id']:03d}",
        "client": eng["client"],
        "mode": eng["mode"],
        "completed": bool(eng["delivered_at"]),
        "consultant_hours": eng["consultant_hours"],
        "fell_back_to_manual": bool(eng["fell_back_to_manual"]),
    }
    if eng["mode"] == "agent":
        unaided = eng["intake_completed_unaided"]
        record.update(
            register_size=register_size,
            intake_completed_unaided=None if unaided is None else bool(unaided),
            intake_submitted_at=eng["intake_submitted_at"],
            draft_pack_ready_at=eng["draft_pack_ready_at"],
            findings=[
                {
                    "id": f"F-{f['id']}",
                    "obligation_id": f["obligation_id"],
                    "status": f["status"],
                    "citations": db.finding_citations(f),
                    "verdict": f["verdict"],
                    "hallucination": bool(f["hallucination"]),
                }
                for f in findings_of(conn, eng["id"])
            ],
            documents=[
                {
                    "type": d["type"],
                    "outcome": d["outcome"],
                    "reviewed_by": d["reviewed_by"],
                    "reviewed_at": d["reviewed_at"],
                }
                for d in documents_of(conn, eng["id"])
            ],
        )
    return record


def scorecard(request: Request, conn: sqlite3.Connection):
    size = len(request.app.state.register.obligations)
    rows = conn.execute("SELECT * FROM engagements ORDER BY id").fetchall()
    records = [parse_engagement(kpi_record(conn, e, size)) for e in rows]
    return build_scorecard(records, request.app.state.index)


User = Annotated[str, Depends(current_user)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


# ----------------------------------------------------------------- routes


def _routes(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return redirect(exc.headers["Location"])
        return render(request, "error.html", status_code=exc.status_code, message=exc.detail)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # ---- auth

    @app.get("/login")
    def login_page(request: Request):
        if request.session.get("user"):
            return redirect("/")
        return render(request, "login.html")

    @app.post("/login")
    async def login(request: Request, conn: Conn):
        form = await form_with_csrf(request)
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        failures = request.app.state.login_failures
        key = username.lower()
        count, window_start = failures.get(key, (0, 0.0))
        if time.monotonic() - window_start >= LOCKOUT_SECONDS:
            count, window_start = 0, time.monotonic()
        if count >= MAX_LOGIN_FAILURES:
            return render(
                request,
                "login.html",
                status_code=429,
                error="Too many failed attempts. Wait 5 minutes and try again.",
            )

        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        ok = verify_password(password, row["password_hash"] if row else DUMMY_HASH) and row
        if not ok:
            failures[key] = (count + 1, window_start)
            db.audit(conn, username or "-", "login_failed")
            return render(
                request, "login.html", status_code=401, error="Wrong username or password."
            )

        failures.pop(key, None)
        request.session.clear()  # new session on login
        request.session["user"] = row["username"]
        request.session["csrf"] = new_csrf_token()
        db.audit(conn, row["username"], "login")
        return redirect("/")

    @app.post("/logout")
    async def logout(request: Request, user: User):
        await form_with_csrf(request)
        request.app.state.agents.pop(user, None)
        request.session.clear()
        return redirect("/login")

    # ---- dashboard

    @app.get("/")
    def dashboard(
        request: Request,
        user: User,
        conn: Conn,
    ):
        card = scorecard(request, conn)
        engagements = conn.execute("SELECT * FROM engagements ORDER BY id DESC").fetchall()
        summaries = [_summary(conn, e) for e in engagements]
        activity = conn.execute(
            "SELECT a.*, e.client FROM audit_log a LEFT JOIN engagements e "
            "ON e.id = a.engagement_id ORDER BY a.id DESC LIMIT 12"
        ).fetchall()
        return render(
            request,
            "dashboard.html",
            card=card,
            summaries=summaries[:8],
            activity=activity,
            in_progress=sum(1 for s in summaries if s["mode"] == "agent" and not s["delivered"]),
            delivered=sum(1 for s in summaries if s["mode"] == "agent" and s["delivered"]),
        )

    # ---- engagements

    @app.get("/engagements")
    def engagement_list(
        request: Request,
        user: User,
        conn: Conn,
    ):
        rows = conn.execute("SELECT * FROM engagements ORDER BY id DESC").fetchall()
        return render(
            request,
            "engagements.html",
            summaries=[_summary(conn, e) for e in rows],
            sectors=SECTORS,
        )

    @app.post("/engagements")
    async def engagement_create(
        request: Request,
        user: User,
        conn: Conn,
    ):
        form = await form_with_csrf(request)
        client = str(form.get("client", "")).strip()
        sector = form.get("sector")
        mode = form.get("mode", "agent")
        if not client or sector not in SECTORS or mode not in {"agent", "manual"}:
            flash(request, "Enter a client name and choose a sector.", "error")
            return redirect("/engagements")
        cur = conn.execute(
            "INSERT INTO engagements (client, sector, mode, created_by, created_at) "
            "VALUES (?,?,?,?,?)",
            (client, sector, mode, user, db.now()),
        )
        db.audit(conn, user, "engagement_created", cur.lastrowid, {"mode": mode})
        return redirect(f"/engagements/{cur.lastrowid}")

    @app.get("/engagements/{eid}")
    def engagement_overview(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        eng = get_engagement(conn, eid)
        activity = conn.execute(
            "SELECT * FROM audit_log WHERE engagement_id = ? ORDER BY id DESC LIMIT 20", (eid,)
        ).fetchall()
        checks = delivery_checks(conn, eng)
        return render(
            request,
            "engagement.html",
            eng=eng,
            s=_summary(conn, eng),
            checks=checks,
            can_deliver=all(ok for _, ok in checks),
            activity=activity,
            tab="overview",
        )

    @app.post("/engagements/{eid}/details")
    async def engagement_details(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        hours_raw = str(form.get("consultant_hours", "")).strip()
        try:
            hours = float(hours_raw) if hours_raw else None
            if hours is not None and hours < 0:
                raise ValueError
        except ValueError:
            flash(request, "Consultant hours must be a positive number.", "error")
            return redirect(f"/engagements/{eid}")
        unaided = {"yes": 1, "no": 0}.get(form.get("intake_completed_unaided", ""))
        fallback = 1 if form.get("fell_back_to_manual") == "on" else 0
        conn.execute(
            "UPDATE engagements SET consultant_hours = ?, intake_completed_unaided = ?, "
            "fell_back_to_manual = ? WHERE id = ?",
            (hours, unaided, fallback, eid),
        )
        db.audit(
            conn,
            user,
            "details_updated",
            eid,
            {"hours": hours, "unaided": unaided, "fallback": fallback},
        )
        flash(request, "Details saved.")
        return redirect(f"/engagements/{eid}")

    @app.post("/engagements/{eid}/deliver")
    async def engagement_deliver(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        if eng["delivered_at"]:
            return redirect(f"/engagements/{eid}")
        failed = [label for label, ok in delivery_checks(conn, eng) if not ok]
        if failed:
            flash(request, "Can't deliver yet: " + "; ".join(failed) + ".", "error")
            return redirect(f"/engagements/{eid}")
        conn.execute("UPDATE engagements SET delivered_at = ? WHERE id = ?", (db.now(), eid))
        db.audit(conn, user, "delivered", eid)
        flash(request, "Engagement marked as delivered.")
        return redirect(f"/engagements/{eid}")

    @app.get("/engagements/{eid}/export.json")
    def engagement_export(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        eng = get_engagement(conn, eid)
        record = kpi_record(conn, eng, len(request.app.state.register.obligations))
        return JSONResponse(
            record,
            headers={"Content-Disposition": f'attachment; filename="{record["id"]}.json"'},
        )

    # ---- intake

    @app.get("/engagements/{eid}/intake")
    def intake_page(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        eng = get_engagement(conn, eid)
        return render(
            request,
            "intake.html",
            eng=eng,
            s=_summary(conn, eng),
            answers=answers_of(conn, eid),
            tab="intake",
        )

    @app.post("/engagements/{eid}/intake")
    async def intake_save(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        form = await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        require_open_agent_engagement(eng)
        register = request.app.state.register
        before = answers_of(conn, eid)
        after: dict[str, str] = {}
        for q in register.questions:
            value = str(form.get(f"q_{q.id}", "")).strip()
            if q.type == "choice" and value not in CHOICES:
                value = ""
            if value:
                after[q.id] = value[:5000]

        conn.execute("DELETE FROM intake_answers WHERE engagement_id = ?", (eid,))
        conn.executemany(
            "INSERT INTO intake_answers (engagement_id, question_id, answer, updated_at) "
            "VALUES (?,?,?,?)",
            [(eid, q, a, db.now()) for q, a in after.items()],
        )
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        if changed and eng["assessed_at"]:
            conn.execute("UPDATE engagements SET stale = 1 WHERE id = ?", (eid,))
            flash(
                request, "Answers changed after the assessment. Re-run it before delivery.", "warn"
            )
        submit = form.get("action") == "submit"
        if submit and not eng["intake_submitted_at"]:
            conn.execute(
                "UPDATE engagements SET intake_submitted_at = ? WHERE id = ?", (db.now(), eid)
            )
        db.audit(
            conn, user, "intake_submitted" if submit else "intake_saved", eid, {"changed": changed}
        )
        flash(request, "Intake submitted." if submit else "Intake saved.")
        return redirect(f"/engagements/{eid}/intake")

    # ---- assessment and findings

    @app.post("/engagements/{eid}/assess")
    async def run_assessment(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        form = await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        require_open_agent_engagement(eng)
        if not eng["intake_submitted_at"]:
            flash(request, "Submit the intake before running the assessment.", "error")
            return redirect(f"/engagements/{eid}/intake")
        register, answers = request.app.state.register, answers_of(conn, eid)
        use_claude = form.get("mode") == "claude"
        if use_claude:
            corpus = request.app.state.corpus
            if corpus is None:
                flash(request, "Build the corpus first (grc-corpus ingest) to use Claude.", "error")
                return redirect(f"/engagements/{eid}/findings")
            try:
                assessor = request.app.state.make_assessor(corpus)
                results = await run_in_threadpool(assessor.assess, register, answers)
            except (TypeError, anthropic.CredentialsError) as exc:
                if isinstance(exc, TypeError) and "authentication method" not in str(exc):
                    raise
                flash(
                    request,
                    "No Claude API credentials. Put ANTHROPIC_API_KEY in .env and restart.",
                    "error",
                )
                return redirect(f"/engagements/{eid}/findings")
            except AssessmentError as exc:
                flash(request, f"Claude assessment failed, nothing was changed: {exc}", "error")
                return redirect(f"/engagements/{eid}/findings")
            except anthropic.APIError as exc:
                flash(
                    request,
                    f"Claude API error ({exc.__class__.__name__}), nothing was changed.",
                    "error",
                )
                return redirect(f"/engagements/{eid}/findings")
        else:
            results = assess(register, answers, request.app.state.index)
        conn.execute("DELETE FROM findings WHERE engagement_id = ?", (eid,))
        conn.executemany(
            "INSERT INTO findings (engagement_id, obligation_id, status, severity, citation, "
            "citation_resolves, summary, remediation, citations_json, unresolved_json, "
            "drafted_by, confidence, needs_legal_review, provisions_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    eid,
                    f.obligation_id,
                    f.status,
                    f.severity,
                    f.citation,
                    int(f.citation_resolves),
                    f.summary,
                    f.remediation,
                    json.dumps(list(f.citations)),
                    json.dumps(list(f.unresolved)),
                    f.drafted_by,
                    f.confidence or None,
                    int(f.needs_legal_review),
                    json.dumps(list(f.provisions)),
                )
                for f in results
            ],
        )
        # Documents are derived from findings, so they must be regenerated and re-reviewed.
        removed = conn.execute("DELETE FROM documents WHERE engagement_id = ?", (eid,)).rowcount
        conn.execute(
            "UPDATE engagements SET assessed_at = ?, stale = 0 WHERE id = ?", (db.now(), eid)
        )
        unresolved = sum(not f.citation_resolves for f in results)
        db.audit(
            conn,
            user,
            "assessed_with_claude" if use_claude else "assessed",
            eid,
            {"findings": len(results), "unresolved": unresolved, "documents_cleared": removed},
        )
        if removed:
            flash(
                request,
                "Assessment re-run. Documents were cleared: generate and review them again.",
                "warn",
            )
        if unresolved:
            flash(request, f"{unresolved} citation(s) don't resolve. Delivery is blocked.", "error")
        else:
            flash(request, f"Assessment complete: {len(results)} findings.")
        return redirect(f"/engagements/{eid}/findings")

    @app.get("/engagements/{eid}/findings")
    def findings_page(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        eng = get_engagement(conn, eid)
        obligations = {o.id: o for o in request.app.state.register.obligations}
        rows = []
        for f in findings_of(conn, eid):
            unresolved = set(db.finding_unresolved(f))
            rows.append(
                {
                    **dict(f),
                    "cites": [(c, c not in unresolved) for c in db.finding_citations(f)],
                    "provisions": json.loads(f["provisions_json"] or "[]"),
                }
            )
        return render(
            request,
            "findings.html",
            eng=eng,
            s=_summary(conn, eng),
            findings=rows,
            corpus_ready=request.app.state.corpus is not None,
            index_source=request.app.state.index_source,
            obligations=obligations,
            verdicts=sorted(VERDICTS),
            tab="findings",
        )

    @app.post("/engagements/{eid}/findings/{fid}")
    async def score_finding(
        eid: int,
        fid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        form = await form_with_csrf(request)
        get_engagement(conn, eid)
        verdict = form.get("verdict") or None
        if verdict is not None and verdict not in VERDICTS:
            raise HTTPException(status_code=400, detail="Unknown verdict")
        hallucination = 1 if form.get("hallucination") == "on" else 0
        if hallucination and verdict != "wrong":
            flash(
                request,
                "A hallucinated finding is always scored wrong, so the verdict was set to wrong.",
                "warn",
            )
            verdict = "wrong"
        updated = conn.execute(
            "UPDATE findings SET verdict = ?, hallucination = ? WHERE id = ? AND engagement_id = ?",
            (verdict, hallucination, fid, eid),
        ).rowcount
        if not updated:
            raise HTTPException(status_code=404, detail="Finding not found")
        db.audit(
            conn,
            user,
            "finding_scored",
            eid,
            {"finding": fid, "verdict": verdict, "hallucination": hallucination},
        )
        return redirect(f"/engagements/{eid}/findings#f{fid}")

    # ---- documents and review gate

    @app.post("/engagements/{eid}/documents/generate")
    async def generate_documents(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        require_open_agent_engagement(eng)
        if not eng["assessed_at"] or eng["stale"]:
            flash(request, "Run the assessment on the current answers first.", "error")
            return redirect(f"/engagements/{eid}/findings")
        facts = EngagementFacts(
            client=eng["client"],
            sector=eng["sector"],
            answers=answers_of(conn, eid),
            findings=[dict(f) for f in findings_of(conn, eid)],
        )
        conn.execute("DELETE FROM documents WHERE engagement_id = ?", (eid,))
        generated = db.now()
        for kind in DOCUMENT_TYPES:
            blocks = build_document(kind, facts, request.app.state.register)
            conn.execute(
                "INSERT INTO documents (engagement_id, type, content_json, generated_at) "
                "VALUES (?,?,?,?)",
                (eid, kind, json.dumps([b.__dict__ for b in blocks]), generated),
            )
        if not eng["draft_pack_ready_at"]:
            conn.execute(
                "UPDATE engagements SET draft_pack_ready_at = ? WHERE id = ?", (generated, eid)
            )
        db.audit(conn, user, "documents_generated", eid, {"count": len(DOCUMENT_TYPES)})
        flash(request, "Draft pack generated. Every document now needs a review.")
        return redirect(f"/engagements/{eid}/documents")

    @app.get("/engagements/{eid}/documents")
    def documents_page(
        eid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        eng = get_engagement(conn, eid)
        return render(
            request,
            "documents.html",
            eng=eng,
            s=_summary(conn, eng),
            documents=documents_of(conn, eid),
            tab="documents",
        )

    def _document(conn: sqlite3.Connection, eid: int, did: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND engagement_id = ?", (did, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return row

    @app.get("/engagements/{eid}/documents/{did}")
    def document_page(
        eid: int,
        did: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        eng = get_engagement(conn, eid)
        doc = _document(conn, eid, did)
        blocks = [Block(**b) for b in json.loads(doc["content_json"])]
        return render(
            request,
            "document.html",
            eng=eng,
            s=_summary(conn, eng),
            doc=doc,
            blocks=blocks,
            outcomes=[o for o in OUTCOME_LABELS if o in DOCUMENT_OUTCOMES],
            tab="documents",
        )

    @app.post("/engagements/{eid}/documents/{did}/review")
    async def review_document(
        eid: int,
        did: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        form = await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        require_open_agent_engagement(eng)
        _document(conn, eid, did)
        outcome = form.get("outcome")
        if outcome not in DOCUMENT_OUTCOMES or form.get("confirm") != "on":
            flash(request, "Choose an outcome and confirm you read the whole document.", "error")
            return redirect(f"/engagements/{eid}/documents/{did}")
        conn.execute(
            "UPDATE documents SET outcome = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (outcome, user, db.now(), did),
        )
        db.audit(conn, user, "document_reviewed", eid, {"document": did, "outcome": outcome})
        flash(request, "Review recorded.")
        return redirect(f"/engagements/{eid}/documents")

    @app.get("/engagements/{eid}/documents/{did}/download")
    def download_document(
        eid: int,
        did: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        eng = get_engagement(conn, eid)
        doc = _document(conn, eid, did)
        if not doc["reviewed_at"]:
            raise HTTPException(status_code=403, detail="Review this document before exporting it.")
        blocks = [Block(**b) for b in json.loads(doc["content_json"])]
        db.audit(conn, user, "document_exported", eid, {"document": did})
        safe_client = "".join(c if c.isalnum() else "-" for c in eng["client"]).strip("-")[:40]
        return Response(
            to_docx(blocks),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_client}-{doc["type"]}.docx"'
            },
        )

    # ---- corpus

    @app.get("/corpus")
    def corpus_page(request: Request, user: User, q: str = ""):
        corpus = request.app.state.corpus
        report = None
        if corpus is not None and (corpus.build_dir / "report.json").exists():
            report = json.loads((corpus.build_dir / "report.json").read_text("utf-8"))
        hits = corpus.search(q, 8) if corpus is not None and q.strip() else []
        return render(
            request,
            "corpus.html",
            corpus=corpus,
            report=report,
            q=q,
            hits=hits,
            index_source=request.app.state.index_source,
        )

    @app.get("/corpus/provision")
    def provision_page(request: Request, user: User, ref: str = ""):
        corpus = request.app.state.corpus
        chunks = corpus.provision(ref) if corpus is not None else []
        return render(
            request,
            "provision.html",
            ref=ref,
            chunks=chunks,
            corpus=corpus,
            resolves=request.app.state.index.resolves(ref),
        )

    # ---- public docs and blog (no login; published items only)

    def public_url(request: Request) -> str:
        return (os.getenv("GRC_PUBLIC_URL") or str(request.base_url)).rstrip("/")

    def published(conn: sqlite3.Connection, type_: str) -> list[sqlite3.Row]:
        order = "position, title" if type_ == "docs" else "published_at DESC"
        return conn.execute(
            f"SELECT * FROM content WHERE type = ? AND status = 'published' ORDER BY {order}",
            (type_,),
        ).fetchall()

    def live_pages(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT type, slug FROM content WHERE status = 'published'")
        return {f"/{r['type']}/{r['slug']}" for r in rows}

    def render_public(request: Request, name: str, status_code: int = 200, **ctx: Any):
        ctx.setdefault("site_name", os.getenv("GRC_SITE_NAME", "GRC agent"))
        ctx.setdefault("base", public_url(request))
        return render(request, name, status_code=status_code, **ctx)

    @app.get("/docs")
    def docs_index(request: Request, conn: Conn):
        return render_public(
            request, "public_list.html", type_="docs", items=published(conn, "docs")
        )

    @app.get("/blog")
    def blog_index(request: Request, conn: Conn):
        return render_public(
            request, "public_list.html", type_="blog", items=published(conn, "blog")
        )

    def public_item(request: Request, conn: sqlite3.Connection, type_: str, slug: str):
        item = conn.execute(
            "SELECT * FROM content WHERE type = ? AND slug = ? AND status = 'published'",
            (type_, slug),
        ).fetchone()
        if item is None:
            return render_public(
                request, "error.html", status_code=404, message="That page doesn't exist."
            )
        return render_public(
            request,
            "public_item.html",
            item=item,
            html=render_markdown(item["body_md"], live_pages(conn)),
            minutes=reading_minutes(item["body_md"]),
            docs=published(conn, "docs"),
            recent=published(conn, "blog")[:5],
        )

    @app.get("/docs/{slug}")
    def docs_item(slug: str, request: Request, conn: Conn):
        return public_item(request, conn, "docs", slug)

    @app.get("/blog/{slug}")
    def blog_item(slug: str, request: Request, conn: Conn):
        return public_item(request, conn, "blog", slug)

    @app.get("/sitemap.xml")
    def sitemap(request: Request, conn: Conn):
        base = public_url(request)
        urls = [(f"{base}/docs", None), (f"{base}/blog", None)]
        for type_ in CONTENT_TYPES:
            urls += [
                (f"{base}/{type_}/{r['slug']}", r["updated_at"][:10])
                for r in published(conn, type_)
            ]
        body = "".join(
            f"<url><loc>{loc}</loc>{f'<lastmod>{mod}</lastmod>' if mod else ''}</url>"
            for loc, mod in urls
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
        )
        return Response(xml, media_type="application/xml")

    @app.get("/robots.txt")
    def robots(request: Request):
        text = (
            "User-agent: *\nAllow: /docs\nAllow: /blog\nAllow: /static/\nDisallow: /\n"
            f"Sitemap: {public_url(request)}/sitemap.xml\n"
        )
        return Response(text, media_type="text/plain")

    # ---- content editor (login required)

    @app.get("/content")
    def content_list(request: Request, user: User, conn: Conn):
        rows = conn.execute("SELECT * FROM content ORDER BY type, position, title").fetchall()
        live = live_pages(conn)
        items = []
        for r in rows:
            checks = seo_checks(dict(r), live)
            items.append({**dict(r), "seo_ok": sum(c.ok for c in checks), "seo_total": len(checks)})
        return render(request, "content_list.html", items=items)

    def _content_form(form: dict[str, Any], type_: str) -> tuple[dict[str, Any], list[str]]:
        data = {
            "type": type_,
            "title": str(form.get("title", "")).strip()[:200],
            "slug": str(form.get("slug", "")).strip().lower()[:80],
            "description": str(form.get("description", "")).strip()[:300],
            "keyword": str(form.get("keyword", "")).strip()[:100],
            "body_md": str(form.get("body_md", ""))[:100_000],
            "reviewed_by": str(form.get("reviewed_by", "")).strip()[:100] or None,
        }
        try:
            data["position"] = int(form.get("position") or 100)
        except ValueError:
            data["position"] = 100
        if not data["slug"]:
            data["slug"] = slugify(data["title"])
        errors = []
        if not data["title"]:
            errors.append("Add a title.")
        if not valid_slug(data["slug"]):
            errors.append("The URL slug can only use lowercase letters, numbers and hyphens.")
        return data, errors

    @app.get("/content/new")
    def content_new(request: Request, user: User, type: str = "blog"):
        if type not in CONTENT_TYPES:
            raise HTTPException(status_code=404, detail="Unknown content type")
        item = {
            "id": None,
            "type": type,
            "title": "",
            "slug": "",
            "description": "",
            "keyword": "",
            "body_md": "",
            "status": "draft",
            "position": 100,
            "reviewed_by": "",
        }
        return render(request, "content_edit.html", item=item, checks=seo_checks(item), preview="")

    @app.post("/content")
    async def content_create(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        type_ = form.get("type")
        if type_ not in CONTENT_TYPES:
            raise HTTPException(status_code=400, detail="Unknown content type")
        data, errors = _content_form(form, type_)
        taken = conn.execute(
            "SELECT 1 FROM content WHERE type = ? AND slug = ?", (type_, data["slug"])
        ).fetchone()
        if taken:
            errors.append(f"There's already a {type_} item at /{type_}/{data['slug']}.")
        if errors:
            for e in errors:
                flash(request, e, "error")
            item = {**data, "id": None, "status": "draft"}
            return render(
                request,
                "content_edit.html",
                item=item,
                checks=seo_checks(item),
                preview=render_markdown(data["body_md"]),
                status_code=400,
            )
        cur = conn.execute(
            "INSERT INTO content (type, slug, title, description, keyword, body_md, position, "
            "author, reviewed_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                type_,
                data["slug"],
                data["title"],
                data["description"],
                data["keyword"],
                data["body_md"],
                data["position"],
                user,
                data["reviewed_by"],
                db.now(),
                db.now(),
            ),
        )
        db.audit(conn, user, "content_created", detail={"id": cur.lastrowid, "slug": data["slug"]})
        flash(request, "Draft saved.")
        return redirect(f"/content/{cur.lastrowid}")

    def _content_row(conn: sqlite3.Connection, cid: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM content WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Content not found")
        return row

    @app.get("/content/{cid}")
    def content_edit(cid: int, request: Request, user: User, conn: Conn):
        item = dict(_content_row(conn, cid))
        return render(
            request,
            "content_edit.html",
            item=item,
            checks=seo_checks(item, live_pages(conn)),
            preview=render_markdown(item["body_md"]),
        )

    @app.post("/content/{cid}")
    async def content_save(cid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        row = _content_row(conn, cid)
        data, errors = _content_form(form, row["type"])
        taken = conn.execute(
            "SELECT 1 FROM content WHERE type = ? AND slug = ? AND id != ?",
            (row["type"], data["slug"], cid),
        ).fetchone()
        if taken:
            errors.append(f"There's already a {row['type']} item at /{row['type']}/{data['slug']}.")
        action = form.get("action", "save")
        status = row["status"]
        if action == "publish":
            if not data["reviewed_by"]:
                errors.append(
                    "Add who reviewed this before publishing. Public pages carry our name."
                )
            if not data["description"]:
                errors.append("Add a meta description before publishing.")
            status = "published"
        elif action == "unpublish":
            status = "draft"
        if errors:
            for e in errors:
                flash(request, e, "error")
            item = {**dict(row), **data}
            return render(
                request,
                "content_edit.html",
                item=item,
                checks=seo_checks(item),
                preview=render_markdown(data["body_md"]),
                status_code=400,
            )
        published_at = row["published_at"] or (db.now() if status == "published" else None)
        conn.execute(
            "UPDATE content SET slug=?, title=?, description=?, keyword=?, body_md=?, position=?, "
            "reviewed_by=?, status=?, updated_at=?, published_at=? WHERE id=?",
            (
                data["slug"],
                data["title"],
                data["description"],
                data["keyword"],
                data["body_md"],
                data["position"],
                data["reviewed_by"],
                status,
                db.now(),
                published_at,
                cid,
            ),
        )
        event = {"publish": "content_published", "unpublish": "content_unpublished"}
        db.audit(
            conn, user, event.get(action, "content_saved"), detail={"id": cid, "slug": data["slug"]}
        )
        flash(
            request,
            {"publish": "Published.", "unpublish": "Unpublished. It's a draft again."}.get(
                action, "Saved."
            ),
        )
        return redirect(f"/content/{cid}")

    # ---- KPIs

    @app.get("/kpis")
    def kpis_page(
        request: Request,
        user: User,
        conn: Conn,
    ):
        return render(request, "kpis.html", card=scorecard(request, conn))

    # ---- assistant

    @app.get("/assistant")
    def assistant_page(request: Request, user: User):
        agent = request.app.state.agents.get(user)
        return render(request, "assistant.html", history=_chat_history(agent))

    @app.post("/assistant")
    async def assistant_ask(request: Request, user: User):
        form = await form_with_csrf(request)
        question = str(form.get("question", "")).strip()[:4000]
        if not question:
            return redirect("/assistant")
        agents = request.app.state.agents
        agent = agents.get(user) or agents.setdefault(user, Agent())
        try:
            result = await run_in_threadpool(agent.ask, question)
            if result.tool_calls:
                flash(request, "Tools used: " + ", ".join(result.tool_calls))
        except (TypeError, anthropic.CredentialsError) as exc:
            if isinstance(exc, TypeError) and "authentication method" not in str(exc):
                raise
            flash(
                request,
                "No Claude API credentials. Put ANTHROPIC_API_KEY in .env and restart, "
                "or set GRC_AI_MODE=demo to test without a key.",
                "error",
            )
        except anthropic.AuthenticationError:
            flash(request, "The Claude API rejected the API key.", "error")
        except anthropic.APIError as exc:
            flash(request, f"Claude API error: {exc.__class__.__name__}. Try again.", "error")
        return redirect("/assistant")

    @app.post("/assistant/reset")
    async def assistant_reset(request: Request, user: User):
        await form_with_csrf(request)
        request.app.state.agents.pop(user, None)
        return redirect("/assistant")


def _summary(conn: sqlite3.Connection, eng: sqlite3.Row) -> dict[str, Any]:
    findings = [dict(f) for f in findings_of(conn, eng["id"])]
    docs = documents_of(conn, eng["id"])
    if eng["delivered_at"]:
        stage = "Delivered"
    elif eng["mode"] == "manual":
        stage = "Manual baseline"
    elif docs and all(d["reviewed_at"] for d in docs):
        stage = "Ready to deliver"
    elif docs:
        stage = "In review"
    elif eng["assessed_at"]:
        stage = "Assessed"
    elif eng["intake_submitted_at"]:
        stage = "Intake submitted"
    else:
        stage = "Intake"
    return {
        "id": eng["id"],
        "client": eng["client"],
        "sector": eng["sector"],
        "mode": eng["mode"],
        "stage": stage,
        "delivered": bool(eng["delivered_at"]),
        "stale": bool(eng["stale"]),
        "score": readiness_score(findings) if findings else None,
        "gaps": sum(f["status"] == "gap" for f in findings),
        "open_items": sum(f["status"] == "open_item" for f in findings),
        "unresolved": sum(not f["citation_resolves"] for f in findings),
        "documents": len(docs),
        "reviewed": sum(1 for d in docs if d["reviewed_at"]),
    }


def _chat_history(agent: Agent | None) -> list[tuple[str, str]]:
    """User questions and Claude's text replies, skipping tool traffic."""
    if agent is None:
        return []
    out = []
    for m in agent.messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            out.append(("user", m["content"]))
        elif m["role"] == "assistant":
            text = "\n".join(b.text for b in m["content"] if b.type == "text").strip()
            if text:
                out.append(("assistant", text))
    return out
