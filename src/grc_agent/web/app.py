"""FastAPI app. Server-rendered HTML, SQLite storage, session login.

Workflow per engagement (build plan, Step 6):
intake -> deterministic assessment -> documents -> human review -> delivery.
The hard stop (6.4): an engagement can't be delivered while any citation fails
to resolve, the assessment is stale, or any document is unreviewed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import anthropic
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware

from grc_agent.agent import Agent
from grc_agent.ai_assessment import AssessmentError, ClaudeAssessor
from grc_agent.assessment import assess, readiness_score
from grc_agent.citations import CorpusIndex
from grc_agent.config import Settings
from grc_agent.connectors import BY_ID as CONNECTORS
from grc_agent.connectors.secrets import SecretBox
from grc_agent.corpus import load_corpus
from grc_agent.documents import DOCUMENT_TYPES, Block, EngagementFacts, build_document, to_docx
from grc_agent.llm import PROVIDERS
from grc_agent.markdown import render_markdown
from grc_agent.prompts import ANALYST_PROMPT
from grc_agent.register import CHOICES, corpus_index_path, load_register
from grc_agent.risk import summary as risk_summary
from grc_agent.web import (
    ai_views,
    analyst,
    connector_views,
    dataflow_views,
    datamanager,
    db,
    demo_tenant,
    discovery_views,
    https,
    mcp_views,
    notification_views,
    notify,
    ops_views,
    register_views,
    risk_views,
)
from grc_agent.web.security import (
    DUMMY_HASH,
    csrf_matches,
    new_csrf_token,
    verify_password,
)

HERE = Path(__file__).parent
FOCUS = "[Focus: "  # prefix on questions asked with a client selected

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
    db_path = db.database_target(data_dir)
    db.init_db(db_path)

    app = FastAPI(
        title="GRC Flow",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_demo_lifespan(data_dir, db_path),
    )
    app.state.db_path = db_path
    app.state.login_failures = {}
    app.state.agents = {}
    app.state.demo = Settings.from_env().demo  # also loads .env; refreshed below
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
    app.state.make_assessor = _assessor_factory(app)
    app.state.connectors = dict(CONNECTORS)
    app.state.secret_box = SecretBox.for_data_dir(data_dir)
    ai_views.refresh(app)  # the AI provider: saved on /settings/ai, else from .env
    app.state.data_dir = data_dir
    app.state.demo_tenant = demo_tenant.is_demo(data_dir)
    app.state.secret_key = _secret_key(data_dir)
    app.add_middleware(
        SessionMiddleware,
        secret_key=app.state.secret_key,
        session_cookie="grc_session",
        max_age=SESSION_SECONDS,
        same_site="strict",
        https_only=https.https_enabled(),  # Secure cookie whenever HTTPS is on
    )
    https.install(app)  # security headers, HSTS, optional redirect to HTTPS
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    _routes(app)
    connector_views.register(app)
    notification_views.register(app)
    dataflow_views.register(app)
    risk_views.register(app)
    discovery_views.register(app)
    register_views.register(app)
    ops_views.register(app)
    ai_views.register(app)
    mcp_views.register(app)
    return app


_ESCAPED_BR = re.compile(r"&lt;br\s*/?&gt;", re.IGNORECASE)


def _chat_html(text: str) -> str:
    """An analyst reply as HTML. Models often put <br> inside table cells; Markdown
    escapes it (raw HTML is off, for safety), so turn exactly that tag back into a break."""
    return _ESCAPED_BR.sub("<br>", render_markdown(text))


# How a reviewer scores an AI-drafted finding, and how much a drafted document needed.
VERDICTS = {"correct", "incomplete", "wrong"}
DOCUMENT_OUTCOMES = {"usable", "minor_edits", "material_edit", "full_rewrite"}

NO_AI_KEY = (
    "No AI provider is set up. Add a free or paid key on the AI provider page "
    "(Settings → AI provider), or put one in .env and restart."
)


def _ai_error(exc: Exception) -> str:
    """A short, safe description of a provider error: its class and first line."""
    first = str(exc).splitlines()[0][:160] if str(exc) else ""
    return f"{exc.__class__.__name__}: {first}" if first else exc.__class__.__name__


def _assessor_factory(app: FastAPI):
    def make(corpus):
        settings, client = ai_views.client_for(app)
        return ClaudeAssessor(corpus, client=client, settings=settings)

    return make


def _demo_lifespan(data_dir: Path, db_path: Path):
    """In the demo tenant only, colleagues "act" every so often so the demo looks live."""
    live = float(os.getenv("GRC_DEMO_LIVE_SECONDS", demo_tenant.LIVE_SECONDS))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if demo_tenant.is_demo(data_dir) and live > 0:
            task = asyncio.create_task(demo_tenant.live_loop(db_path, live))
        try:
            yield
        finally:
            if task:
                task.cancel()

    return lifespan


templates = Jinja2Templates(directory=HERE / "templates")


def describe_action(row: Any) -> str:
    """A plain-English phrase for an audit entry, e.g. "moved REQ-001 to Closed"."""
    action = row["action"]
    try:
        d = json.loads(row["detail"]) if row["detail"].startswith("{") else {}
    except (ValueError, AttributeError):
        d = {}
    ref, title = d.get("ref", ""), d.get("title", "")
    phrases = {
        "record_created": f"added {ref}" + (f" “{title}”" if title else ""),
        "record_status": f"moved {ref} to {d.get('label', d.get('to', ''))}",
        "record_updated": f"edited {ref}",
        "record_comment": f"commented on {ref}",
        "control_updated": f"set {d.get('source', '')} to {d.get('label', '')}",
        "evidence_uploaded": f"uploaded evidence “{d.get('title', '')}”",
        "evidence_replaced": f"uploaded a new version of “{d.get('title', '')}”",
        "evidence_deleted": f"deleted evidence “{d.get('title', '')}”",
        "evidence_downloaded": f"downloaded “{d.get('title', '')}”",
        "scan_completed": f"scanned {d.get('source', 'a file')}",
        "finding_confirmed": f"confirmed {d.get('field', '')} as personal data",
        "finding_rejected": f"marked {d.get('field', '')} as not personal data",
        "inventory_updated": f"documented {d.get('field', '')} in the inventory",
    }
    return phrases.get(action) or action.replace("_", " ")


templates.env.filters["activity"] = describe_action


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


# Paths a read-only (viewer) account may still post to.
# A read-only key (MCP tools only read) is fine for a viewer too.
VIEWER_POSTS = ("/login", "/logout", "/notifications", "/settings/api-keys")


def user_role(request: Request, username: str | None = None) -> str:
    username = username or request.session.get("user")
    if not username:
        return ""
    with db.connect(request.app.state.db_path) as c:
        row = c.execute(
            "SELECT role FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        ).fetchone()
    return row["role"] if row else ""


async def form_with_csrf(request: Request) -> dict[str, Any]:
    form = await request.form()
    if not csrf_matches(request.session.get("csrf"), form.get("csrf")):
        raise HTTPException(status_code=403, detail="Form expired. Go back, reload and try again.")
    if user_role(request) == "viewer" and not request.url.path.startswith(VIEWER_POSTS):
        raise HTTPException(
            status_code=403, detail="Your account is read-only. Ask an admin for access."
        )
    return {k: form.getlist(k) if len(form.getlist(k)) > 1 else form[k] for k in form}


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    if "csrf" not in request.session:
        request.session["csrf"] = new_csrf_token()
    user = request.session.get("user")
    unread = 0
    if user:
        with db.connect(request.app.state.db_path) as c:
            unread = notify.unread_count(c, user)
    context.update(
        request=request,
        user=user,
        unread=unread,
        csrf=request.session["csrf"],
        flashes=request.session.pop("flash", []),
        register=request.app.state.register,
        demo_mode=request.app.state.demo,
        ai_label=request.app.state.ai.provider_label,
        ai_names={k: p.label for k, p in PROVIDERS.items()} | {"claude": "Claude"},
        document_types=DOCUMENT_TYPES,
        choices=CHOICES,
        outcome_labels=OUTCOME_LABELS,
        demo_tenant=request.app.state.demo_tenant,
        demo_login=(demo_tenant.DEMO_USER, demo_tenant.DEMO_PASSWORD)
        if request.app.state.demo_tenant
        else None,
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def get_engagement(conn: sqlite3.Connection, engagement_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM engagements WHERE id = ?", (engagement_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Engagement not found")
    return row


def require_open_agent_engagement(eng: sqlite3.Row) -> None:
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


def is_ai_drafted(finding) -> bool:
    """Written by a language model (not the rules engine, not demo placeholder text)."""
    return finding["drafted_by"] not in ("rules", "demo")


def delivery_checks(conn: sqlite3.Connection, eng: sqlite3.Row) -> list[tuple[str, bool]]:
    """The hard stop. Every check must pass before delivery; there is no override."""
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
        (
            "Intake answers match connector evidence",
            not connector_views.conflicts(conn, eng["id"], answers_of(conn, eng["id"]), {}),
        ),
        # AI drafts, people decide: nothing an AI wrote reaches a client unreviewed, and a
        # draft the reviewer called wrong must be rewritten by a person first.
        (
            "Every AI-drafted finding reviewed by a person",
            all(f["verdict"] for f in findings if is_ai_drafted(f)),
        ),
        (
            "Every finding marked wrong has been rewritten",
            all(f["edited_at"] for f in findings if f["verdict"] == "wrong"),
        ),
        # Demo-mode text is a placeholder, never AI output, so it can't go to a client.
        (
            "No demo-mode (placeholder) findings",
            not any(f["drafted_by"] == "demo" for f in findings),
        ),
    ]


def engagement_record(
    conn: sqlite3.Connection, eng: sqlite3.Row, register_size: int
) -> dict[str, Any]:
    """An engagement's findings and documents as JSON, for export."""
    record: dict[str, Any] = {
        "id": f"ENG-{eng['id']:03d}",
        "client": eng["client"],
        "completed": bool(eng["delivered_at"]),
    }
    record.update(
        register_size=register_size,
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
                username=username,
            )

        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        ).fetchone()
        ok = verify_password(password, row["password_hash"] if row else DUMMY_HASH) and row
        if not ok:
            failures[key] = (count + 1, window_start)
            db.audit(conn, username or "-", "login_failed")
            return render(
                request,
                "login.html",
                status_code=401,
                error="Wrong username or password.",
                username=username,
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
        engagements = conn.execute("SELECT * FROM engagements ORDER BY id DESC").fetchall()
        summaries = [_summary(conn, e) for e in engagements]
        agent = summaries
        scores = [s["score"] for s in agent if s["score"] is not None]
        activity = conn.execute(
            "SELECT a.*, e.client FROM audit_log a LEFT JOIN engagements e "
            "ON e.id = a.engagement_id WHERE a.action NOT IN ('login', 'logout') "
            "ORDER BY a.id DESC LIMIT 8"
        ).fetchall()
        connections = conn.execute(
            "SELECT c.connector, c.status, c.message, c.engagement_id, e.client FROM connections c "
            "JOIN engagements e ON e.id = c.engagement_id"
        ).fetchall()
        return render(
            request,
            "dashboard.html",
            summaries=summaries[:8],
            total=len(summaries),
            activity=activity,
            in_progress=sum(1 for s in agent if not s["delivered"]),
            delivered=sum(1 for s in agent if s["delivered"]),
            avg_readiness=round(sum(scores) / len(scores)) if scores else None,
            pipeline=_pipeline(agent),
            attention=_attention(
                agent,
                connections,
                discovery_views.by_engagement(conn),
                register_views.queue(conn, limit=200),
            ),
            work=register_views.queue(conn, limit=200),
            risk_queue=analyst.queue(conn, request.app),
            leaves_india=sum(
                1
                for e in engagements
                if analyst.flow_for(conn, request.app, e)["summary"]["leaves_india"]
            ),
            connections_ok=sum(1 for c in connections if c["status"] == "ok"),
            connections_total=len(connections),
            store=datamanager.report(
                conn,
                request.app.state.db_path,
                check_integrity=False,
                data_dir=request.app.state.data_dir,
            ),
            personal=discovery_views.summary(conn),
            personal_by_client=discovery_views.by_engagement(conn),
        )

    # ---- data manager (read-only storage monitor)

    @app.get("/data-manager")
    def data_manager(request: Request, user: User, conn: Conn):
        return render(
            request,
            "data_manager.html",
            store=datamanager.report(
                conn, request.app.state.db_path, data_dir=request.app.state.data_dir
            ),
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
        if not client or sector not in SECTORS:
            flash(request, "Enter a client name and choose a sector.", "error")
            return redirect("/engagements")
        cur = conn.execute(
            "INSERT INTO engagements (client, sector, mode, created_by, created_at) "
            "VALUES (?,?,'agent',?,?)",
            (client, sector, user, db.now()),
        )
        db.audit(conn, user, "engagement_created", cur.lastrowid)
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
        risks = analyst.risks_for(conn, request.app, eid)
        snapshot = {
            "risks": risk_summary(risks),
            "top": [r for r in risks if r.status != "closed"][:3],
            "flow": analyst.flow_for(conn, request.app, eng)["summary"],
        }
        return render(
            request,
            "engagement.html",
            eng=eng,
            s=_summary(conn, eng),
            snapshot=snapshot,
            checks=checks,
            can_deliver=all(ok for _, ok in checks),
            conflicts=connector_views.conflicts(
                conn, eid, answers_of(conn, eid), request.app.state.connectors
            ),
            activity=activity,
            tab="overview",
        )

    @app.post("/engagements/{eid}/deliver")
    async def engagement_deliver(
        eid: int,
        request: Request,
        user: User,
        background: BackgroundTasks,
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
        connector_views.queue_notification(
            request, background, conn, eid, f"{eng['client']}: engagement delivered by {user}."
        )
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
        record = engagement_record(conn, eng, len(request.app.state.register.obligations))
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
        background: BackgroundTasks,
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
        first_submit = submit and not eng["intake_submitted_at"]
        if first_submit:
            conn.execute(
                "UPDATE engagements SET intake_submitted_at = ? WHERE id = ?", (db.now(), eid)
            )
        db.audit(
            conn, user, "intake_submitted" if submit else "intake_saved", eid, {"changed": changed}
        )
        if first_submit:
            connector_views.queue_notification(
                request, background, conn, eid, f"{eng['client']}: intake submitted."
            )
        flash(request, "Intake submitted." if submit else "Intake saved.")
        return redirect(f"/engagements/{eid}/intake")

    # ---- assessment and findings

    @app.post("/engagements/{eid}/assess")
    async def run_assessment(
        eid: int,
        request: Request,
        user: User,
        background: BackgroundTasks,
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
                flash(request, NO_AI_KEY, "error")
                return redirect(f"/engagements/{eid}/findings")
            except AssessmentError as exc:
                flash(request, f"AI assessment failed, nothing was changed: {exc}", "error")
                return redirect(f"/engagements/{eid}/findings")
            except anthropic.APIError as exc:
                flash(
                    request,
                    f"AI provider error ({_ai_error(exc)}), nothing was changed.",
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
        connector_views.queue_notification(
            request,
            background,
            conn,
            eid,
            f"{eng['client']}: assessment run by {user}, {len(results)} findings"
            + (f", {unresolved} unresolved citation(s)." if unresolved else "."),
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
        evidence = connector_views.evidence_rows(conn, eid)
        rows = []
        for f in findings_of(conn, eid):
            unresolved = set(db.finding_unresolved(f))
            o = obligations.get(f["obligation_id"])
            linked = connector_views.evidence_for_provision(evidence, o.source) if o else []
            rows.append(
                {
                    **dict(f),
                    "cites": [(c, c not in unresolved) for c in db.finding_citations(f)],
                    "provisions": json.loads(f["provisions_json"] or "[]"),
                    "evidence": {
                        st: sum(e["status"] == st for e in linked)
                        for st in ("pass", "fail", "warn")
                    },
                }
            )
        return render(
            request,
            "findings.html",
            eng=eng,
            s=_summary(conn, eng),
            findings=rows,
            conflicts=connector_views.conflicts(
                conn, eid, answers_of(conn, eid), request.app.state.connectors
            ),
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
        reviewed = (user, db.now()) if verdict else (None, None)
        updated = conn.execute(
            "UPDATE findings SET verdict = ?, hallucination = ?, reviewed_by = ?, reviewed_at = ? "
            "WHERE id = ? AND engagement_id = ?",
            (verdict, hallucination, *reviewed, fid, eid),
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

    @app.post("/engagements/{eid}/findings/{fid}/edit")
    async def edit_finding(
        eid: int,
        fid: int,
        request: Request,
        user: User,
        conn: Conn,
    ):
        """A person rewrites a finding's text. Status, severity and citations stay as the
        rules and the citation check set them."""
        form = await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        require_open_agent_engagement(eng)
        summary = str(form.get("summary", "")).strip()[:2000]
        remediation = str(form.get("remediation", "")).strip()[:2000]
        if not summary:
            flash(request, "The finding can't be empty.", "error")
            return redirect(f"/engagements/{eid}/findings#f{fid}")
        row = conn.execute(
            "SELECT status FROM findings WHERE id = ? AND engagement_id = ?", (fid, eid)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Finding not found")
        conn.execute(
            "UPDATE findings SET summary = ?, remediation = ?, edited_by = ?, edited_at = ? "
            "WHERE id = ?",
            (summary, remediation if row["status"] != "compliant" else "", user, db.now(), fid),
        )
        # The draft pack quotes findings, so it has to be regenerated and reviewed again.
        removed = conn.execute("DELETE FROM documents WHERE engagement_id = ?", (eid,)).rowcount
        db.audit(conn, user, "finding_edited", eid, {"finding": fid, "documents_cleared": removed})
        flash(
            request,
            "Finding updated."
            + (" Documents were cleared: generate and review them again." if removed else ""),
            "warn" if removed else "info",
        )
        return redirect(f"/engagements/{eid}/findings#f{fid}")

    # ---- documents and review gate

    @app.post("/engagements/{eid}/documents/generate")
    async def generate_documents(
        eid: int,
        request: Request,
        user: User,
        background: BackgroundTasks,
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
        connector_views.queue_notification(
            request, background, conn, eid, f"{eng['client']}: draft pack ready for review."
        )
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

    @app.get("/robots.txt")
    def robots():
        # Everything here is behind a login; ask search engines to stay out.
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    # ---- assistant

    def analyst_agent(request: Request, user: str) -> Agent:
        agents = request.app.state.agents
        if user not in agents:
            settings, client = ai_views.client_for(request.app)
            agents[user] = Agent(
                client=client,
                settings=settings,
                # Viewers get the read-only tools; everyone else may also draft tasks.
                tools=analyst.analyst_tools(
                    request.app, user=user, can_act=user_role(request) != "viewer"
                ),
                system_prompt=ANALYST_PROMPT,
            )
        return agents[user]

    @app.get("/assistant")
    def assistant_page(request: Request, user: User, conn: Conn):
        agent = request.app.state.agents.get(user)
        # One bubble per reply, even when Claude wrote text before and after using a tool.
        merged: list[list[str]] = []
        for role, text in _chat_history(agent):
            if merged and role == "assistant" and merged[-1][0] == "assistant":
                merged[-1][1] += "\n\n" + text
            else:
                merged.append([role, text])
        history = []
        for role, text in merged:
            if role == "assistant":
                history.append((role, Markup(_chat_html(text)), ""))
            else:
                focus, _, question = (
                    text.partition("\n") if text.startswith(FOCUS) else ("", "", text)
                )
                history.append((role, question, focus.removeprefix(FOCUS).rstrip("]")))
        engagements = conn.execute("SELECT id, client FROM engagements ORDER BY id DESC").fetchall()
        focus_id = request.session.get("analyst_focus")
        focus = next((e for e in engagements if e["id"] == focus_id), None)
        return render(
            request,
            "assistant.html",
            history=history,
            engagements=engagements,
            focus=focus,
            queue=analyst.queue(conn, request.app)[:6],
        )

    @app.post("/assistant/focus")
    async def assistant_focus(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        value = str(form.get("focus", ""))
        request.session["analyst_focus"] = int(value) if value.isdigit() else None
        return redirect("/assistant")

    @app.post("/assistant")
    async def assistant_ask(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        question = str(form.get("question", "")).strip()[:4000]
        if not question:
            return redirect("/assistant")
        focus_id = request.session.get("analyst_focus")
        focus = conn.execute(
            "SELECT id, client FROM engagements WHERE id = ?", (focus_id,)
        ).fetchone()
        if focus:  # the analyst sees which client the question is about
            question = f"{FOCUS}ENG-{focus['id']:03d} {focus['client']}]\n{question}"
        agent = analyst_agent(request, user)
        try:
            result = await run_in_threadpool(agent.ask, question)
            if result.tool_calls:
                flash(request, "Tools used: " + ", ".join(dict.fromkeys(result.tool_calls)))
        except (TypeError, anthropic.CredentialsError) as exc:
            if isinstance(exc, TypeError) and "authentication method" not in str(exc):
                raise
            flash(request, NO_AI_KEY, "error")
        except anthropic.AuthenticationError:
            flash(
                request,
                f"{request.app.state.ai.provider_label} rejected the API key. "
                "Check it on the AI provider page.",
                "error",
            )
        except anthropic.RateLimitError:
            flash(
                request,
                "The AI provider's rate limit was hit. Wait a minute and ask again.",
                "error",
            )
        except anthropic.APIError as exc:
            flash(request, f"AI provider error: {_ai_error(exc)}. Try again.", "error")
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
        "stage": stage,
        "delivered": bool(eng["delivered_at"]),
        "stale": bool(eng["stale"]),
        "score": readiness_score(findings) if findings else None,
        "gaps": sum(f["status"] == "gap" for f in findings),
        "open_items": sum(f["status"] == "open_item" for f in findings),
        "unresolved": sum(not f["citation_resolves"] for f in findings),
        "documents": len(docs),
        "reviewed": sum(1 for d in docs if d["reviewed_at"]),
        "open": register_views.counts(conn, eng["id"]),
        "personal_pending": conn.execute(
            "SELECT COUNT(*) FROM scan_findings WHERE engagement_id = ? AND status = 'pending'",
            (eng["id"],),
        ).fetchone()[0],
    }


STAGES = ("Intake", "Intake submitted", "Assessed", "In review", "Ready to deliver", "Delivered")


def _pipeline(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How many agent engagements sit at each stage, in workflow order."""
    counts = {stage: 0 for stage in STAGES}
    for s in summaries:
        if s["stage"] in counts:
            counts[s["stage"]] += 1
    top = max(counts.values(), default=0) or 1
    return [{"stage": k, "count": v, "pct": round(100 * v / top)} for k, v in counts.items()]


def _attention(
    summaries: list[dict[str, Any]],
    connections: list,
    personal: list[dict[str, Any]] = (),
    work: list[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """What needs someone's action next, most urgent first."""
    items = []
    for s in summaries:
        if s["delivered"]:
            continue
        link = f"/engagements/{s['id']}"
        if s["stale"]:
            items.append(
                {
                    "level": "serious",
                    "client": s["client"],
                    "href": f"{link}/findings",
                    "text": "Intake changed after the assessment. Re-run it.",
                }
            )
        if s["unresolved"]:
            items.append(
                {
                    "level": "critical",
                    "client": s["client"],
                    "href": f"{link}/findings",
                    "text": f"{s['unresolved']} finding(s) cite a provision that doesn't resolve.",
                }
            )
        if s["documents"] and s["reviewed"] < s["documents"]:
            items.append(
                {
                    "level": "warning",
                    "client": s["client"],
                    "href": f"{link}/documents",
                    "text": f"{s['documents'] - s['reviewed']} document(s) waiting for review.",
                }
            )
        if s["stage"] == "Ready to deliver":
            items.append(
                {
                    "level": "good",
                    "client": s["client"],
                    "href": link,
                    "text": "Everything reviewed. Ready to deliver.",
                }
            )
        if s["stage"] == "Intake submitted":
            items.append(
                {
                    "level": "warning",
                    "client": s["client"],
                    "href": f"{link}/findings",
                    "text": "Intake is in. Run the assessment.",
                }
            )
    for c in connections:
        if c["status"] == "error":
            name = (
                CONNECTORS[c["connector"]].name if c["connector"] in CONNECTORS else c["connector"]
            )
            items.append(
                {
                    "level": "serious",
                    "client": c["client"],
                    "href": f"/engagements/{c['engagement_id']}/connectors",
                    "text": f"{name} connector failed: {c['message'] or 'check it'}",
                }
            )
    for w in work:
        link = f"/engagements/{w['engagement_id']}/r/{w['register']}/{w['id']}"
        if w["register"] == "breaches":
            items.append(
                {
                    "level": "critical",
                    "client": w["client"],
                    "href": link,
                    "text": f"Breach {w['ref']}: {w['title']}. Board report "
                    + ("overdue." if w["overdue"] else f"due {w['due'].replace('T', ' ')}."),
                }
            )
        elif w["overdue"]:
            items.append(
                {
                    "level": "serious",
                    "client": w["client"],
                    "href": link,
                    "text": f"Overdue {w['spec'].singular} {w['ref']}: {w['title']}",
                }
            )
    for p in personal:
        if p["pending"]:
            n = p["pending"]
            items.append(
                {
                    "level": "warning",
                    "client": p["client"],
                    "href": f"/engagements/{p['id']}/discovery",
                    "text": f"{n} personal data finding{'s' if n != 1 else ''} to review.",
                }
            )
    order = {"critical": 0, "serious": 1, "warning": 2, "good": 3}
    return sorted(items, key=lambda i: order[i["level"]])


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
