"""Data manager: keeps an eye on what the workspace stores and where.

It only reads. It lists every table with its purpose, whether it holds
personal data, how many rows it has and how old they are, then raises
watch items (unknown tables, records past their suggested retention, a
fragmented file, a failed integrity check). Backups, purges and retention
settings are planned; their buttons are in the UI but not wired yet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    "content": {
        "purpose": "Docs and blog posts",
        "category": "Content",
        "personal": False,
        "retain_days": None,
        "retention": "Until unpublished",
    },
    "content_seeds": {
        "purpose": "Which starter articles have been loaded",
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


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_stats(conn: sqlite3.Connection, today: datetime | None = None) -> list[TableStat]:
    today = today or datetime.now(timezone.utc)
    stats = []
    for name in _tables(conn):
        q = _quote(name)
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({q})")}
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
    db_path: Path,
    today: datetime | None = None,
    check_integrity: bool = True,
) -> dict:
    """Everything the Data manager page shows. Read-only.

    The integrity check reads the whole file, so the dashboard skips it.
    """
    db_path = Path(db_path)
    tables = table_stats(conn, today)
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
    integrity = (
        conn.execute("PRAGMA quick_check").fetchone()[0] if check_integrity else "not checked"
    )
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    db_bytes = _size(db_path)
    wal_bytes = _size(db_path.with_name(db_path.name + "-wal"))
    files = sorted(
        (
            {"name": p.name, "bytes": _size(p), "size": human_size(_size(p))}
            for p in db_path.parent.iterdir()
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
                    f"Suggested retention is {t.retention.lower()}. Purge is planned.",
                )
            )
    if free_pct >= 20 and page_count > 100:
        watch.append(
            Watch("warning", f"{free_pct}% of the file is free space", "A VACUUM would shrink it.")
        )
    watch.append(
        Watch(
            "warning",
            "No backups yet",
            "Scheduled backups are planned. Copy the data folder by hand until then.",
        )
    )

    personal = [t for t in tables if t.personal]
    return {
        "db_path": str(db_path),
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
