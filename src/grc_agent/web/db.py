"""Storage: a SQLite file by default, or PostgreSQL when GRC_DATABASE_URL is set.

The schema below is written for SQLite; pg.py adapts it (and the app's queries) for
PostgreSQL, so every other module works the same on either database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from grc_agent.web import pg

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engagements (
    id INTEGER PRIMARY KEY,
    client TEXT NOT NULL,
    sector TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'agent' CHECK (mode IN ('agent', 'manual')),  -- always 'agent' now
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    intake_submitted_at TEXT,
    assessed_at TEXT,
    stale INTEGER NOT NULL DEFAULT 0,
    draft_pack_ready_at TEXT,
    delivered_at TEXT,
    -- 'client': a GRC partner assessing a client; 'self': a company assessing itself.
    audience TEXT NOT NULL DEFAULT 'client',
    -- No longer used (pilot measurements); kept so older workspaces still load and migrate.
    consultant_hours REAL,
    intake_completed_unaided INTEGER,
    fell_back_to_manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS intake_answers (
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    question_id TEXT NOT NULL,
    answer TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (engagement_id, question_id)
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    obligation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    citation TEXT NOT NULL,
    citation_resolves INTEGER NOT NULL,
    summary TEXT NOT NULL,
    remediation TEXT NOT NULL,
    verdict TEXT,
    hallucination INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    type TEXT NOT NULL,
    content_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    outcome TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('docs', 'blog')),
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    keyword TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
    author TEXT NOT NULL,
    reviewed_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE (type, slug)
);
-- Starter content already imported, so a renamed or edited item is never re-imported.
CREATE TABLE IF NOT EXISTS content_seeds (
    type TEXT NOT NULL,
    slug TEXT NOT NULL,
    PRIMARY KEY (type, slug)
);
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    connector TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',   -- non-secret settings, shown in the app
    secrets_enc TEXT NOT NULL,                -- credentials, encrypted (connectors/secrets.py)
    secret_hints_json TEXT NOT NULL DEFAULT '{}',  -- masked, e.g. "••••abcd"
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    message TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    connection_id INTEGER NOT NULL REFERENCES connections(id),
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    check_key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    provisions_json TEXT NOT NULL DEFAULT '[]',
    data_json TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    engagement_id INTEGER,
    category TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('info', 'good', 'warning', 'serious', 'critical')),
    title TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notification_reads (
    username TEXT NOT NULL COLLATE NOCASE,
    notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    PRIMARY KEY (username, notification_id)
);
-- Client data-flow map: systems and vendors a consultant adds on top of the generated map.
CREATE TABLE IF NOT EXISTS dataflow_nodes (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    name TEXT NOT NULL,
    stage TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT 'unknown',
    categories TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Consultant edits to the generated risk register, keyed by risk (e.g. "finding:OBL-004").
CREATE TABLE IF NOT EXISTS risk_edits (
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    risk_key TEXT NOT NULL,
    likelihood INTEGER CHECK (likelihood BETWEEN 1 AND 5),
    impact INTEGER CHECK (impact BETWEEN 1 AND 5),
    treatment TEXT,
    owner TEXT,
    due TEXT,
    status TEXT,
    notes TEXT,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (engagement_id, risk_key)
);
-- Personal data discovery. A scan reads an uploaded file in memory; the file itself is
-- never stored. Findings keep only metadata and masked value shapes.
CREATE TABLE IF NOT EXISTS scan_jobs (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    source_name TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    columns INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed')),
    progress INTEGER NOT NULL DEFAULT 0,
    engine TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    personal_fields INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS scan_findings (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scan_jobs(id),
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    source_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    risk TEXT NOT NULL,
    confidence TEXT NOT NULL,
    match_ratio REAL NOT NULL,
    sampled INTEGER NOT NULL,
    entities_json TEXT NOT NULL DEFAULT '{}',
    shapes_json TEXT NOT NULL DEFAULT '[]',
    minors INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'rejected', 'superseded')),
    note TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT,
    reviewed_at TEXT
);
-- The client's inventory of personal data: one row per field that holds it.
CREATE TABLE IF NOT EXISTS data_inventory (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    finding_id INTEGER REFERENCES scan_findings(id),
    source_name TEXT NOT NULL,
    field TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    risk TEXT NOT NULL,
    children INTEGER NOT NULL DEFAULT 0,
    purpose TEXT NOT NULL DEFAULT '',
    principals TEXT NOT NULL DEFAULT '',
    legal_basis TEXT NOT NULL DEFAULT '',
    retention TEXT NOT NULL DEFAULT '',
    storage_location TEXT NOT NULL DEFAULT '',
    recipients TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (engagement_id, source_name, field)
);
-- Registers (tasks, consent, requests, breaches, vendors, DPIAs, policies): one row per
-- record, fields a register defines in data_json. Every change is kept in record_events.
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    register TEXT NOT NULL,
    ref TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    due TEXT NOT NULL DEFAULT '',
    obligation_id TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (engagement_id, register, ref)
);
CREATE INDEX IF NOT EXISTS records_by_register ON records (engagement_id, register, status);
CREATE TABLE IF NOT EXISTS record_events (
    id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    at TEXT NOT NULL,
    username TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('created', 'status', 'edited', 'comment')),
    detail_json TEXT NOT NULL DEFAULT '{}'
);
-- Compliance controls: the client's own status for each obligation in the register.
CREATE TABLE IF NOT EXISTS controls (
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    obligation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_started' CHECK (status IN
        ('not_started', 'in_progress', 'needs_review', 'implemented', 'not_applicable')),
    owner TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    na_reason TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    review_date TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (engagement_id, obligation_id)
);
-- Uploaded evidence files. The file lives in <data dir>/evidence/, never in the web root.
CREATE TABLE IF NOT EXISTS evidence_files (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    obligation_id TEXT NOT NULL DEFAULT '',
    record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    replaces_id INTEGER REFERENCES evidence_files(id),
    status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current', 'superseded')),
    review_date TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_providers (
    provider TEXT PRIMARY KEY,                -- a key of grc_agent.llm.PROVIDERS
    model TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    key_enc TEXT NOT NULL DEFAULT '',         -- API key, encrypted like connector secrets
    key_hint TEXT NOT NULL DEFAULT '',        -- masked, e.g. "••••abcd"
    active INTEGER NOT NULL DEFAULT 0,        -- at most one row is active
    models_json TEXT NOT NULL DEFAULT '[]',   -- the provider's model ids at the last test
    status TEXT NOT NULL DEFAULT '',          -- '', 'ok' or 'error' from the last test
    message TEXT NOT NULL DEFAULT '',
    tested_at TEXT,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- A customer organisation (web/access.py): a company running its own DPDP work
-- ('client') or a consultancy running it for others ('partner'). A POC has an end date.
CREATE TABLE IF NOT EXISTS orgs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'client' CHECK (kind IN ('client', 'partner')),
    poc_until TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Who may open a client besides staff and the person who created it (web/access.py):
-- a partner given a client, or a client's own people.
CREATE TABLE IF NOT EXISTS engagement_access (
    id INTEGER PRIMARY KEY,
    engagement_id INTEGER NOT NULL REFERENCES engagements(id),
    username TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    UNIQUE (engagement_id, username)
);
-- Keys that let AI apps read the workspace over MCP (web/mcp_views.py). Only a hash is kept.
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    hint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
-- Google / Microsoft accounts linked to a GRC Flow account (web/auth_views.py). A sign-in
-- is matched on (provider, subject) only, never on an email address.
CREATE TABLE IF NOT EXISTS login_identities (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    provider TEXT NOT NULL,                   -- google | microsoft
    subject TEXT NOT NULL,                    -- the provider's stable id for the person
    email TEXT NOT NULL DEFAULT '',           -- shown on the Account page only
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    UNIQUE (provider, subject)
);
-- Single-use links sent by email: invites and password resets. Only a hash is kept.
CREATE TABLE IF NOT EXISTS auth_tokens (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    purpose TEXT NOT NULL,                    -- invite | reset
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    username TEXT NOT NULL,
    engagement_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database_target(data_dir: str | Path) -> str | Path:
    """Where the workspace's data lives: GRC_DATABASE_URL (PostgreSQL) or <data_dir>/grc.db.

    With PostgreSQL, GRC_DATABASE_SCHEMA picks a schema; "auto" derives one from the data
    folder, so separate workspaces (and tests) sharing one server never see each other's data.
    """
    url = os.getenv("GRC_DATABASE_URL", "").strip()
    if not url:
        return Path(data_dir) / "grc.db"
    if not pg.is_postgres(url):
        raise SystemExit("GRC_DATABASE_URL must start with postgresql:// (or postgres://).")
    schema = os.getenv("GRC_DATABASE_SCHEMA", "").strip()
    if schema == "auto":
        digest = hashlib.sha1(
            str(Path(data_dir).resolve()).encode(), usedforsecurity=False
        ).hexdigest()[:16]
        schema = f"ws_{digest}"
    return pg.with_schema(url, schema) if schema else url


def is_postgres(target: object) -> bool:
    return pg.is_postgres(target)


def label(target: str | Path) -> str:
    """Where the data is, safe to show on screen (no password)."""
    return pg.safe_label(target) if pg.is_postgres(target) else str(target)


class _ClosingConnection(sqlite3.Connection):
    """`with connect(...) as conn:` commits (or rolls back) and then closes, like the
    PostgreSQL connection does. Plain sqlite3 leaves the file open until garbage
    collection, and Windows can't delete or replace a file that is still open."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path: str | Path) -> sqlite3.Connection:
    # One connection per request. FastAPI may open it in a worker thread and use
    # it in the event loop thread, but never from two threads at once.
    if pg.is_postgres(path):
        return pg.Connection(path)  # type: ignore[return-value]
    conn = sqlite3.connect(path, check_same_thread=False, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. init_db adds any that are missing, so an
# existing local database upgrades in place.
MIGRATIONS = {
    # Platform role: super_admin, admin or user (see web/access.py).
    "users": {
        "role": "TEXT NOT NULL DEFAULT 'admin'",
        # No longer used: POC end dates live on the organisation (orgs.poc_until).
        "expires_at": "TEXT NOT NULL DEFAULT ''",
        # The customer organisation and the person's role in its team.
        "org_id": "INTEGER",
        "team_role": "TEXT NOT NULL DEFAULT 'admin'",
        # Where invites and password-reset links go (optional).
        "email": "TEXT NOT NULL DEFAULT ''",
    },
    "findings": {
        "citations_json": "TEXT",
        "unresolved_json": "TEXT",
        "drafted_by": "TEXT NOT NULL DEFAULT 'rules'",
        "confidence": "TEXT",
        "needs_legal_review": "INTEGER NOT NULL DEFAULT 0",
        "provisions_json": "TEXT",
        # Human review of AI-drafted findings: who scored it, and who rewrote it.
        "reviewed_by": "TEXT",
        "reviewed_at": "TEXT",
        "edited_by": "TEXT",
        "edited_at": "TEXT",
    },
    "ai_providers": {"models_json": "TEXT NOT NULL DEFAULT '[]'"},
    # Who the workspace is for; changes the wording, not the rules (see AUDIENCES in app.py).
    "engagements": {
        "audience": "TEXT NOT NULL DEFAULT 'client'",
        "org_id": "INTEGER",  # the organisation whose team works on it
    },
    # AI relevance check of an evidence file (see grc_agent.evidence_check).
    "evidence_files": {
        "ai_check": "TEXT NOT NULL DEFAULT ''",
        "ai_check_reason": "TEXT NOT NULL DEFAULT ''",
        "ai_check_missing": "TEXT NOT NULL DEFAULT '[]'",
        "ai_checked_by": "TEXT NOT NULL DEFAULT ''",
        "ai_checked_at": "TEXT",
        "check_overruled_by": "TEXT NOT NULL DEFAULT ''",
    },
    # Tamper evidence: each entry carries the hash of the one before it (see audit()).
    "audit_log": {"prev_hash": "TEXT", "hash": "TEXT"},
}


def init_db(path: str | Path) -> None:
    if pg.is_postgres(path):
        _init_postgres(path)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
        _data_fixes(conn)


def _data_fixes(conn) -> None:
    """One-off data changes that keep old workspaces working with current code."""
    # The "manual baseline" engagement mode was removed; those become ordinary engagements.
    conn.execute("UPDATE engagements SET mode = 'agent' WHERE mode <> 'agent'")
    # Roles before partners and clients: a member ran every client, a viewer read every
    # client. Each keeps seeing the clients that exist now, then takes the new role.
    for old, new in (("member", "partner"), ("viewer", "client")):
        conn.execute(
            "INSERT INTO engagement_access (engagement_id, username, granted_by, granted_at) "
            "SELECT e.id, u.username, 'upgrade', ? FROM engagements e, users u "
            "WHERE u.role = ? AND NOT EXISTS (SELECT 1 FROM engagement_access a "
            "WHERE a.engagement_id = e.id AND LOWER(a.username) = LOWER(u.username))",
            (now(), old),
        )
        conn.execute("UPDATE users SET role = ? WHERE role = ?", (new, old))
    # Partners, clients and trials become organisations: each such account gets its own
    # (a trial's end date becomes the POC's) and is its team admin, and the engagements
    # it created belong to that organisation.
    for row in conn.execute(
        "SELECT id, username, role, expires_at FROM users "
        "WHERE role IN ('partner', 'client', 'trial')"
    ).fetchall():
        cur = conn.execute(
            "INSERT INTO orgs (name, kind, poc_until, created_by, created_at) VALUES (?,?,?,?,?)",
            (
                row["username"],
                "partner" if row["role"] == "partner" else "client",
                row["expires_at"] if row["role"] == "trial" else "",
                "upgrade",
                now(),
            ),
        )
        conn.execute(
            "UPDATE users SET role = 'user', org_id = ?, team_role = 'admin', expires_at = '' "
            "WHERE id = ?",
            (cur.lastrowid, row["id"]),
        )
        conn.execute(
            "UPDATE engagements SET org_id = ? "
            "WHERE org_id IS NULL AND LOWER(created_by) = LOWER(?)",
            (cur.lastrowid, row["username"]),
        )
    # Someone must own the workspace: the first admin becomes its super admin.
    if not conn.execute("SELECT 1 FROM users WHERE role = 'super_admin'").fetchone():
        first = conn.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if first:
            conn.execute("UPDATE users SET role = 'super_admin' WHERE id = ?", (first["id"],))


def _init_postgres(url: str) -> None:
    schema = pg.schema_of(url)
    if schema != "public":
        with pg.Connection(pg.without_schema(url)) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    with connect(url) as conn:
        conn.executescript(pg.pg_schema(SCHEMA))
        # SQLite compares usernames without case (COLLATE NOCASE); keep that rule.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_username_ci ON users (LOWER(username))"
        )
        for table, columns in MIGRATIONS.items():
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ?",
                    (table,),
                )
            }
            for name, spec in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
        _data_fixes(conn)


def finding_citations(row) -> list[str]:
    """A finding's citations; rows from before multi-citation support hold just one."""
    if row["citations_json"]:
        return json.loads(row["citations_json"])
    return [row["citation"]]


def finding_unresolved(row) -> list[str]:
    if row["unresolved_json"] is not None:
        return json.loads(row["unresolved_json"])
    return [] if row["citation_resolves"] else [row["citation"]]


def audit(
    conn: sqlite3.Connection,
    username: str,
    action: str,
    engagement_id: int | None = None,
    detail: str | dict = "",
) -> None:
    data = detail if isinstance(detail, dict) else {}
    if isinstance(detail, dict):
        detail = json.dumps(detail)
    at = now()
    if pg.is_postgres_conn(conn):
        # One writer at a time until commit, so two requests can't both chain onto the
        # same previous entry. SQLite gets the same from its single write lock.
        conn.execute("SELECT pg_advisory_xact_lock(?)", (AUDIT_LOCK,))
    cur = conn.execute(
        "INSERT INTO audit_log (at, username, engagement_id, action, detail) VALUES (?,?,?,?,?)",
        (at, username, engagement_id, action, detail),
    )
    new_id = cur.lastrowid
    prev = conn.execute(
        "SELECT hash FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT 1", (new_id,)
    ).fetchone()
    prev_hash = (prev["hash"] if prev else None) or ""
    conn.execute(
        "UPDATE audit_log SET prev_hash = ?, hash = ? WHERE id = ?",
        (prev_hash, entry_hash(prev_hash, at, username, engagement_id, action, detail), new_id),
    )
    from grc_agent.web import notify  # here to avoid a circular import

    notify.from_audit(conn, username, action, engagement_id, data, detail)


# ---------------------------------------------------------------- tamper evidence

AUDIT_LOCK = 742_001  # any fixed number; names the advisory lock for audit writes


def entry_hash(
    prev_hash: str, at: str, username: str, engagement_id: int | None, action: str, detail: str
) -> str:
    """SHA-256 over the previous entry's hash and this entry's fields.

    Changing, removing or reordering any entry changes every hash after it, so an
    edit made directly in the database (outside the app) shows up when the chain
    is checked. Anyone can recompute it from the CSV export.
    """
    canonical = json.dumps(
        [prev_hash, at, username, engagement_id, action, detail],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ChainReport:
    ok: bool
    checked: int  # entries whose hash was recomputed
    before_chain: int  # older entries written before the chain existed
    first_id: int | None  # where the chain starts
    last_hash: str  # the latest hash: note it down to detect removal of recent entries
    broken_id: int | None = None
    problem: str = ""


def verify_audit_chain(conn: sqlite3.Connection) -> ChainReport:
    """Recompute every hash in order and check each entry points at the one before."""
    checked = before = 0
    first_id: int | None = None
    prev_hash: str | None = None
    for r in conn.execute(
        "SELECT id, at, username, engagement_id, action, detail, prev_hash, hash "
        "FROM audit_log ORDER BY id"
    ):
        if prev_hash is None:
            if not r["hash"]:
                before += 1  # written before chaining; nothing to check
                continue
            first_id = r["id"]  # the chain starts here (older entries may be purged)
        else:
            if not r["hash"]:
                return ChainReport(
                    False,
                    checked,
                    before,
                    first_id,
                    prev_hash,
                    r["id"],
                    "This entry has no hash: it was added outside the app.",
                )
            if r["prev_hash"] != prev_hash:
                return ChainReport(
                    False,
                    checked,
                    before,
                    first_id,
                    prev_hash,
                    r["id"],
                    "This entry doesn't point at the one before it: an entry was removed "
                    "or the order changed.",
                )
        expected = entry_hash(
            r["prev_hash"] or "",
            r["at"],
            r["username"],
            r["engagement_id"],
            r["action"],
            r["detail"],
        )
        if expected != r["hash"]:
            return ChainReport(
                False,
                checked,
                before,
                first_id,
                prev_hash or "",
                r["id"],
                "This entry was changed after it was recorded.",
            )
        prev_hash = r["hash"]
        checked += 1
    return ChainReport(True, checked, before, first_id, prev_hash or "")


# Tables in an order that satisfies their foreign keys, for copying between databases.
COPY_ORDER = (
    "users",
    "engagements",
    "intake_answers",
    "findings",
    "documents",
    "content",
    "content_seeds",
    "connections",
    "evidence",
    "notifications",
    "notification_reads",
    "dataflow_nodes",
    "risk_edits",
    "scan_jobs",
    "scan_findings",
    "data_inventory",
    "records",
    "record_events",
    "controls",
    "evidence_files",
    "ai_providers",
    "orgs",
    "engagement_access",
    "api_keys",
    "login_identities",
    "auth_tokens",
    "audit_log",
)


def copy_to_postgres(sqlite_path: str | Path, url: str) -> dict[str, int]:
    """Copy every row of a SQLite workspace into an empty PostgreSQL database.

    Ids are kept, so links between records survive, and each id sequence is moved past
    the highest copied id. Refuses to write into a database that already has accounts.
    """
    source = Path(sqlite_path)
    if not source.is_file():
        raise SystemExit(f"No SQLite database at {source}.")
    init_db(source)  # bring an older file up to the current schema first
    init_db(url)
    copied: dict[str, int] = {}
    with connect(source) as src, connect(url) as dst:
        if dst.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise SystemExit(
                "The PostgreSQL database already has accounts. Point GRC_DATABASE_URL at an "
                "empty database (or schema) to migrate into."
            )
        for table in COPY_ORDER:
            # Safe: the SQL text holds only names from this code; values are ? parameters.
            rows = src.execute(f"SELECT * FROM {table}").fetchall()  # nosec B608  # noqa: S608
            if rows:
                cols = rows[0].keys()
                sql = (
                    # Safe: the SQL text holds only names from this code; values are ? parameters.
                    f"INSERT INTO {table} ({', '.join(cols)}) "  # nosec B608  # noqa: S608
                    f"VALUES ({', '.join('?' for _ in cols)})"
                )
                for row in rows:
                    dst.execute(sql, tuple(row))
            copied[table] = len(rows)
            if table in pg.ID_TABLES:
                dst.execute(
                    # Safe: the SQL text holds only names from this code; values are ? parameters.
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "  # nosec B608  # noqa: S608
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                    f"(SELECT MAX(id) FROM {table}) IS NOT NULL)"
                )
    return copied


def reset_postgres_schema(url: str) -> None:
    """Empty a workspace's own schema (used by the demo tenant's --reset)."""
    schema = pg.schema_of(url)
    if schema == "public":
        raise SystemExit("Refusing to reset the public schema.")
    with pg.Connection(pg.without_schema(url)) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
