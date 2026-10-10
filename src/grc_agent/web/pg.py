"""PostgreSQL behind the same small interface the app uses for SQLite.

The app talks to its database through `db.connect()` and writes SQLite-flavoured SQL:
`?` placeholders, `INSERT OR IGNORE`, `cursor.lastrowid`, rows you can read by name or
position. This module wraps a psycopg connection so the same code runs on PostgreSQL:

- `?` becomes `%s` (and a literal `%` becomes `%%`);
- `INSERT OR IGNORE INTO t ...` becomes `INSERT INTO t ... ON CONFLICT DO NOTHING`;
- an INSERT into a table with an `id` column gets `RETURNING id`, which fills `lastrowid`;
- Python booleans are sent as 0/1, matching the INTEGER flag columns;
- rows support `row["name"]`, `row[0]`, `dict(row)` and `row.keys()`, like sqlite3.Row.

Set GRC_DATABASE_URL=postgresql://user:password@host:5432/dbname to use it. The
optional GRC_DATABASE_SCHEMA puts the tables in their own schema (tests use one schema
per workspace so they don't see each other's data).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

# Tables whose primary key is a generated `id` (so INSERTs can return it).
ID_TABLES = {
    "users",
    "employees",
    "orgs",
    "engagement_access",
    "engagements",
    "findings",
    "documents",
    "content",
    "connections",
    "evidence",
    "notifications",
    "dataflow_nodes",
    "scan_jobs",
    "scan_findings",
    "data_inventory",
    "records",
    "record_events",
    "evidence_files",
    "api_keys",
    "login_identities",
    "auth_tokens",
    "audit_log",
}

_INSERT = re.compile(r"^\s*INSERT\s+(OR\s+IGNORE\s+)?INTO\s+([A-Za-z_]+)", re.I)


def is_postgres(target: object) -> bool:
    return isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))


def is_postgres_conn(conn: object) -> bool:
    return isinstance(conn, Connection)


def with_schema(url: str, schema: str) -> str:
    """The URL with a search_path option, so every connection lands in `schema`."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError(f"Unsafe schema name: {schema!r}")
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def without_schema(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def schema_of(url: str) -> str:
    for k, v in parse_qsl(urlsplit(url).query):
        if k == "options" and "search_path=" in v:
            return v.split("search_path=", 1)[1].split()[0]
    return "public"


def safe_label(url: str) -> str:
    """Host, port and database for display: never the user name or password."""
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    schema = schema_of(url)
    return f"PostgreSQL · {host}{port}{parts.path}" + (
        f" (schema {schema})" if schema != "public" else ""
    )


def translate(sql: str) -> tuple[str, bool]:
    """(PostgreSQL SQL, whether it returns the new id)."""
    returning = False
    m = _INSERT.match(sql)
    if m:
        if m.group(1):
            sql = _INSERT.sub(lambda mm: f"INSERT INTO {mm.group(2)}", sql, count=1)
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        if m.group(2).lower() in ID_TABLES and "RETURNING" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            returning = True
    sql = sql.replace("%", "%%").replace("?", "%s")
    return sql, returning


class Row(tuple):
    """A result row readable by position or column name, like sqlite3.Row."""

    def __new__(cls, values: Sequence[Any], names: Sequence[str]):
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def keys(self) -> list[str]:
        return list(self._names)

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                raise IndexError(f"No column named {key}") from None
        return tuple.__getitem__(self, key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(values, names)


def _params(params: Sequence[Any] | None) -> tuple | None:
    if not params:
        return None
    return tuple(int(p) if isinstance(p, bool) else p for p in params)


class Cursor:
    def __init__(self, cur, returning: bool):
        self._cur = cur
        self.lastrowid: int | None = None
        self._buffered: list | None = None
        if returning:
            rows = cur.fetchall()
            self.lastrowid = rows[0][0] if rows else None
            self._buffered = []

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def fetchone(self):
        if self._buffered is not None:
            return None
        return self._cur.fetchone() if self._cur.description else None

    def fetchall(self) -> list:
        if self._buffered is not None:
            return []
        return self._cur.fetchall() if self._cur.description else []

    def __iter__(self) -> Iterator:
        return iter(self.fetchall())


class Connection:
    """psycopg connection with the sqlite3 methods the app uses."""

    def __init__(self, url: str):
        try:
            import psycopg
        except ImportError:
            raise SystemExit(
                "GRC_DATABASE_URL is set but PostgreSQL support isn't installed. "
                'Run: pip install -e ".[postgres]" (start.bat and start.sh do this for you).'
            ) from None

        self._conn = psycopg.connect(url, row_factory=_row_factory)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Cursor:
        pg_sql, returning = translate(sql)
        args = _params(params)
        if args is None:
            pg_sql = pg_sql.replace("%%", "%")
        cur = self._conn.cursor()
        cur.execute(pg_sql, args)
        return Cursor(cur, returning)

    def executemany(self, sql: str, seq_of_params) -> None:
        for params in seq_of_params:
            self.execute(sql, params)

    def executescript(self, script: str) -> None:
        lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
        for statement in "\n".join(lines).split(";"):
            if statement.strip():
                self._conn.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    # sqlite3's context manager commits or rolls back; this one also closes, so
    # server connections aren't left open.
    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()


def pg_schema(schema_sql: str) -> str:
    """The app's SQLite schema rewritten for PostgreSQL."""
    out = schema_sql.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")
    return out.replace(" COLLATE NOCASE", "")
