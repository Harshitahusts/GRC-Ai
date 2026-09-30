"""PostgreSQL support: SQL translation, configuration, and migrating a SQLite workspace.

The translation tests always run. The ones that need a server run when
GRC_TEST_DATABASE_URL (or GRC_DATABASE_URL) points at a PostgreSQL database; CI runs the
whole suite against a PostgreSQL service as well.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from helpers import PASSWORD, create, csrf, login, post

from grc_agent.web import db, pg
from grc_agent.web.app import create_app

SERVER = os.getenv("GRC_TEST_DATABASE_URL") or os.getenv("GRC_DATABASE_URL")
needs_server = pytest.mark.skipif(not SERVER, reason="No PostgreSQL server configured")


# ---------------------------------------------------------------- no server needed


@pytest.mark.parametrize(
    ("sql", "expected", "returns_id"),
    [
        ("SELECT * FROM users WHERE id = ?", "SELECT * FROM users WHERE id = %s", False),
        (
            "INSERT INTO audit_log (at, username) VALUES (?,?)",
            "INSERT INTO audit_log (at, username) VALUES (%s,%s) RETURNING id",
            True,
        ),
        (
            "INSERT OR IGNORE INTO notification_reads (username, notification_id) VALUES (?, ?)",
            "INSERT INTO notification_reads (username, notification_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            False,
        ),
        (
            "INSERT OR IGNORE INTO data_inventory (field) VALUES (?)",
            "INSERT INTO data_inventory (field) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id",
            True,
        ),
        (
            "SELECT name FROM t WHERE name LIKE 'x_%' AND id = ?",
            "SELECT name FROM t WHERE name LIKE 'x_%%' AND id = %s",
            False,
        ),
    ],
)
def test_translate(sql, expected, returns_id):
    assert pg.translate(sql) == (expected, returns_id)


def test_rows_read_like_sqlite_rows():
    row = pg.Row((7, "harshit"), ["id", "username"])
    assert row[0] == 7 and row["username"] == "harshit"
    assert dict(row) == {"id": 7, "username": "harshit"} and row.keys() == ["id", "username"]
    with pytest.raises(IndexError):
        row["nope"]


def test_schema_url_and_label_hide_the_password():
    url = "postgresql://grc:s3cret@db.example.in:5433/grc?sslmode=require"
    scoped = pg.with_schema(url, "grc_demo")
    assert pg.schema_of(scoped) == "grc_demo" and "sslmode=require" in scoped
    assert pg.schema_of(pg.without_schema(scoped)) == "public"
    label = pg.safe_label(scoped)
    assert label == "PostgreSQL · db.example.in:5433/grc (schema grc_demo)"
    assert "s3cret" not in label and "grc:" not in label
    with pytest.raises(ValueError):
        pg.with_schema(url, 'x"; DROP TABLE users; --')


def test_database_target(monkeypatch, tmp_path):
    monkeypatch.delenv("GRC_DATABASE_URL", raising=False)
    assert db.database_target(tmp_path) == tmp_path / "grc.db"
    monkeypatch.setenv("GRC_DATABASE_URL", "mysql://nope")
    with pytest.raises(SystemExit):
        db.database_target(tmp_path)
    monkeypatch.setenv("GRC_DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setenv("GRC_DATABASE_SCHEMA", "auto")
    a, b = db.database_target(tmp_path / "a"), db.database_target(tmp_path / "b")
    assert pg.schema_of(a).startswith("ws_") and pg.schema_of(a) != pg.schema_of(b)
    assert db.database_target(tmp_path / "a") == a  # stable


def test_login_ignores_username_case(client):
    token = csrf(client, "/login")
    r = client.post(
        "/login",
        data={"username": "HARSHIT", "password": PASSWORD, "csrf": token},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/"


# ---------------------------------------------------------------- with a server


@pytest.fixture
def pg_url(monkeypatch):
    schema = "t_" + uuid.uuid4().hex[:12]
    url = pg.with_schema(pg.without_schema(SERVER), schema)
    yield url
    db.reset_postgres_schema(url)


@needs_server
def test_migrate_sqlite_workspace_to_postgres(tmp_path, monkeypatch, pg_url):
    from grc_agent.web import cli

    # A real SQLite workspace with an account, an engagement and a register record.
    monkeypatch.delenv("GRC_DATABASE_URL", raising=False)
    monkeypatch.delenv("GRC_DATABASE_SCHEMA", raising=False)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(PASSWORD + "\n"))
    cli.main(["--data-dir", str(tmp_path), "adduser", "harshit", "--password-stdin"])
    old = TestClient(create_app(tmp_path))
    login(old)
    eid = create(old)
    post(old, f"/engagements/{eid}/r/tasks", {"title": "Carry me over", "priority": "high"})

    assert cli.main(["--data-dir", str(tmp_path), "migrate-to-postgres", "--url", pg_url]) == 0
    with db.connect(pg_url) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT title FROM records").fetchone()[0] == "Carry me over"

    # The app now runs on PostgreSQL with the same data, and new ids don't collide.
    monkeypatch.setenv("GRC_DATABASE_URL", pg_url)
    fresh = TestClient(create_app(tmp_path))
    assert login(fresh).status_code == 303
    assert "Carry me over" in fresh.get(f"/engagements/{eid}/r/tasks").text
    assert create(fresh) == eid + 1
    page = fresh.get("/data-manager").text
    assert "PostgreSQL" in page and "Uncatalogued" not in page

    # Migrating again into a database that has accounts is refused.
    with pytest.raises(SystemExit, match="already has accounts"):
        cli.main(["--data-dir", str(tmp_path), "migrate-to-postgres", "--url", pg_url])


@needs_server
def test_usernames_unique_regardless_of_case(pg_url):
    db.init_db(pg_url)
    with db.connect(pg_url) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
            ("Priya", "x", db.now()),
        )
    with pytest.raises(Exception, match="users_username_ci|unique"):
        with db.connect(pg_url) as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                ("priya", "x", db.now()),
            )
