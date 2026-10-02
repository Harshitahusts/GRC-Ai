"""Data manager: keeps an eye on what the workspace stores and where.

It only reads. It lists every table with its purpose, whether it holds
personal data, how many rows it has and how old they are, then raises
watch items (unknown tables, records past their suggested retention, a
fragmented file, a failed integrity check). Backups are up to you: copy the
data folder, or use your PostgreSQL provider's backups.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from grc_agent.web import pg

# What each table is for. `personal` means it holds personal data about
# people (users, client contacts, intake answers), which DPDPA cares about.
# `retain_days` is a suggested retention period, not an enforced one.
CATALOG: dict[str, dict] = {
    "users": {
        "purpose": "Workspace logins (username, password hash)",
        "category": "Access",
        "personal": True,
        "retain_days": None,
        "retention": "While the account is active",
    },
    "engagements": {
        "purpose": "Client engagements and their workflow dates",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "intake_answers": {
        "purpose": "Client answers to the intake questionnaire",
        "category": "Client data",
        "personal": True,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "findings": {
        "purpose": "Assessment findings per DPDPA obligation",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "documents": {
        "purpose": "Generated documents (notices, policies, reports)",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "evidence": {
        "purpose": "Read-only checks collected by connectors",
        "category": "Evidence",
        "personal": False,
        "retain_days": 365,
        "retention": "1 year, then re-collect",
    },
    "connections": {
        "purpose": "Connector settings (secrets are encrypted)",
        "category": "Evidence",
        "personal": False,
        "retain_days": None,
        "retention": "Until the connector is removed",
    },
    "ai_providers": {
        "purpose": "AI provider settings for the GRC Analyst (API keys are encrypted)",
        "category": "System",
        "personal": False,
        "retain_days": None,
        "retention": "Until the provider is removed",
    },
    "dataflow_nodes": {
        "purpose": "Systems added by hand to a client's data-flow map",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "risk_edits": {
        "purpose": "Owner, treatment and score changes to risks",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "scan_jobs": {
        "purpose": "Personal data scans of uploaded files (the files are not kept)",
        "category": "Discovery",
        "personal": False,
        "retain_days": 365,
        "retention": "1 year, then re-scan",
    },
    "scan_findings": {
        "purpose": "Fields found to hold personal data (masked value shapes, no values)",
        "category": "Discovery",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "data_inventory": {
        "purpose": "Client's inventory of personal data fields, purposes and retention",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "records": {
        "purpose": "Tasks, consent, requests, breaches, vendors, DPIAs and policies",
        "category": "Client data",
        "personal": True,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "record_events": {
        "purpose": "History and comments on each register record",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "controls": {
        "purpose": "Client's implementation status per obligation",
        "category": "Client data",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "evidence_files": {
        "purpose": "Uploaded evidence (files are kept in the evidence folder)",
        "category": "Evidence",
        "personal": False,
        "retain_days": None,
        "retention": "Contract term + 3 years",
    },
    "content": {
        "purpose": "Old docs and blog posts (that editor was removed; kept so nothing is lost)",
        "category": "System",
        "personal": False,
        "retain_days": None,
        "retention": "Delete when no longer needed",
    },
    "content_seeds": {
        "purpose": "Which starter articles were loaded (no longer used)",
        "category": "System",
        "personal": False,
        "retain_days": None,
        "retention": "Keep",
    },
    "notifications": {
        "purpose": "Workspace notifications",
        "category": "System",
        "personal": False,
        "retain_days": 90,
        "retention": "90 days",
    },
    "notification_reads": {
        "purpose": "Who has read which notification",
        "category": "System",
        "personal": True,
        "retain_days": 90,
        "retention": "90 days",
    },
    "audit_log": {
        "purpose": "Who did what and when, including logins",
        "category": "Security",
        "personal": True,
        "retain_days": 365,
        "retention": "1 year (DPDP Rules log retention)",
    },
}

# Column that dates each row, first match wins.
_DATE_COLUMNS = ("at", "created_at", "collected_at", "generated_at", "updated_at")


@dataclass
class TableStat:
    name: str
    rows: int
    oldest: str | None
    newest: str | None
    expired: int
    purpose: str
    category: str
    personal: bool
    retention: str
    known: bool


@dataclass
class Watch:
    level: str  # good | warning | serious | critical
    title: str
    detail: str


def _is_pg(conn) -> bool:
    return isinstance(conn, pg.Connection)


def _tables(conn: sqlite3.Connection) -> list[str]:
    if _is_pg(conn):
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]


def _columns(conn, table: str) -> set[str]:
    if _is_pg(conn):
        return {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            )
        }
    return {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_stats(conn: sqlite3.Connection, today: datetime | None = None) -> list[TableStat]:
    today = today or datetime.now(timezone.utc)
    stats = []
    for name in _tables(conn):
        q = _quote(name)
        columns = _columns(conn, name)
        date_col = next((c for c in _DATE_COLUMNS if c in columns), None)
        info = CATALOG.get(name, {})
        rows = conn.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
        oldest = newest = None
        expired = 0
        if date_col and rows:
            oldest, newest = conn.execute(
                f"SELECT MIN({date_col}), MAX({date_col}) FROM {q}"
            ).fetchone()
            if info.get("retain_days"):
                cutoff = (today - timedelta(days=info["retain_days"])).isoformat()
                expired = conn.execute(
                    f"SELECT COUNT(*) FROM {q} WHERE {date_col} < ?", (cutoff,)
                ).fetchone()[0]
        stats.append(
            TableStat(
                name=name,
                rows=rows,
                oldest=oldest,
                newest=newest,
                expired=expired,
                purpose=info.get("purpose", "Not catalogued"),
                category=info.get("category", "Unknown"),
                personal=info.get("personal", False),
                retention=info.get("retention", "Not set"),
                known=name in CATALOG,
            )
        )
    return stats


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # unreachable


def report(
    conn: sqlite3.Connection,
    db_path: Path | str,
    today: datetime | None = None,
    check_integrity: bool = True,
    data_dir: Path | None = None,
) -> dict:
    """Everything the Data manager page shows. Read-only.

    The integrity check reads the whole file, so the dashboard skips it. On PostgreSQL
    the server looks after integrity and free space, so those checks don't apply.
    """
    tables = table_stats(conn, today)
    if _is_pg(conn):
        page_count = free_pages = 0
        integrity = "ok" if check_integrity else "not checked"
        journal = "PostgreSQL"
        db_bytes = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        wal_bytes = 0
        folder = Path(data_dir) if data_dir else None
        where = pg.safe_label(str(db_path))
    else:
        db_path = Path(db_path)
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        integrity = (
            conn.execute("PRAGMA quick_check").fetchone()[0] if check_integrity else "not checked"
        )
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        db_bytes = _size(db_path)
        wal_bytes = _size(db_path.with_name(db_path.name + "-wal"))
        folder = db_path.parent
        where = str(db_path)
    files = sorted(
        (
            {"name": p.name, "bytes": _size(p), "size": human_size(_size(p))}
            for p in (folder.iterdir() if folder and folder.is_dir() else [])
            if p.is_file()
        ),
        key=lambda f: -f["bytes"],
    )
    free_pct = round(100 * free_pages / page_count) if page_count else 0

    watch: list[Watch] = []
    if integrity not in {"ok", "not checked"}:
        watch.append(Watch("critical", "Integrity check failed", str(integrity)))
    for t in tables:
        if not t.known:
            watch.append(
                Watch(
                    "serious",
                    f"Uncatalogued table: {t.name}",
                    "Add its purpose and retention to the data catalogue.",
                )
            )
        if t.expired:
            watch.append(
                Watch(
                    "warning",
                    f"{t.expired} {t.name} row{'s' if t.expired != 1 else ''} past retention",
                    f"Suggested retention is {t.retention.lower()}. "
                    "Review them and delete what is no longer needed.",
                )
            )
    if free_pct >= 20 and page_count > 100:
        watch.append(
            Watch("warning", f"{free_pct}% of the file is free space", "A VACUUM would shrink it.")
        )
    watch.append(
        Watch(
            "warning",
            "Back up regularly",
            "Use your PostgreSQL provider's backups (or pg_dump), and back up the data "
            "folder for the key and evidence files."
            if _is_pg(conn)
            else "Copy the data folder (grc.db, keys and evidence files) to a safe place.",
        )
    )

    personal = [t for t in tables if t.personal]
    return {
        "db_path": where,
        "engine": "PostgreSQL" if _is_pg(conn) else "SQLite",
        "db_size": human_size(db_bytes),
        "wal_size": human_size(wal_bytes),
        "total_size": human_size(db_bytes + wal_bytes),
        "journal": journal,
        "integrity": integrity,
        "free_pct": free_pct,
        "tables": tables,
        "rows": sum(t.rows for t in tables),
        "personal_tables": len(personal),
        "personal_rows": sum(t.rows for t in personal),
        "files": files,
        "watch": watch,
        "healthy": integrity in {"ok", "not checked"}
        and not any(w.level in {"critical", "serious"} for w in watch),
        "checked_at": (today or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }
