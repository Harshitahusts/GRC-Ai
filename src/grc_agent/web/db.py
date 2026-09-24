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
    if isinstance(detail, dict):
        detail = json.dumps(detail)
    conn.execute(
        "INSERT INTO audit_log (at, username, engagement_id, action, detail) VALUES (?,?,?,?,?)",
        (now(), username, engagement_id, action, detail),
    )
