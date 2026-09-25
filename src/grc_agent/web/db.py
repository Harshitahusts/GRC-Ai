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
