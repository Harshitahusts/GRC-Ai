"""SQLite storage. One file, created on first run."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    mode TEXT NOT NULL CHECK (mode IN ('agent', 'manual')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    intake_submitted_at TEXT,
    assessed_at TEXT,
    stale INTEGER NOT NULL DEFAULT 0,
    draft_pack_ready_at TEXT,
    delivered_at TEXT,
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


def connect(path: str | Path) -> sqlite3.Connection:
    # One connection per request. FastAPI may open it in a worker thread and use
    # it in the event loop thread, but never from two threads at once.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. init_db adds any that are missing, so an
# existing local database upgrades in place.
MIGRATIONS = {
    # admin: everything incl. team; member: all client work; viewer: read-only.
    "users": {"role": "TEXT NOT NULL DEFAULT 'admin'"},
    "findings": {
        "citations_json": "TEXT",
        "unresolved_json": "TEXT",
        "drafted_by": "TEXT NOT NULL DEFAULT 'rules'",
        "confidence": "TEXT",
        "needs_legal_review": "INTEGER NOT NULL DEFAULT 0",
        "provisions_json": "TEXT",
    },
}


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def seed_content(conn: sqlite3.Connection, items: list[dict]) -> int:
    """Import starter docs and posts as drafts, once each. Returns how many were added."""
    added = 0
    for item in items:
        seen = conn.execute(
            "SELECT 1 FROM content_seeds WHERE type = ? AND slug = ?", (item["type"], item["slug"])
        ).fetchone()
        if seen:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO content (type, slug, title, description, keyword, body_md, "
            "position, status, author, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?, 'draft', 'starter content', ?, ?)",
            (
                item["type"],
                item["slug"],
                item["title"],
                item["description"],
                item["keyword"],
                item["body_md"],
                item["position"],
                now(),
                now(),
            ),
        )
        conn.execute(
            "INSERT INTO content_seeds (type, slug) VALUES (?, ?)", (item["type"], item["slug"])
        )
        added += 1
    return added


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
    conn.execute(
        "INSERT INTO audit_log (at, username, engagement_id, action, detail) VALUES (?,?,?,?,?)",
        (now(), username, engagement_id, action, detail),
    )
    from grc_agent.web import notify  # here to avoid a circular import

    notify.from_audit(conn, username, action, engagement_id, data, detail)
