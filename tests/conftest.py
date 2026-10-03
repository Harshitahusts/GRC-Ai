"""Shared fixtures: a fresh app with one user, and clients logged in or not."""

import io
import os

import pytest
from fastapi.testclient import TestClient
from helpers import PASSWORD, login

from grc_agent.connectors import ConnectorError
from grc_agent.llm import PROVIDERS
from grc_agent.web import cli as web_cli
from grc_agent.web.app import create_app


@pytest.fixture(scope="session", autouse=True)
def _postgres_isolation():
    """With GRC_DATABASE_URL set, the suite runs on PostgreSQL: every workspace (each
    test's data folder) gets its own schema, dropped when the run ends."""
    url = os.getenv("GRC_DATABASE_URL")
    if not url:
        yield
        return
    os.environ["GRC_DATABASE_SCHEMA"] = "auto"
    yield
    from grc_agent.web import pg

    with pg.Connection(pg.without_schema(url)) as conn:
        for (name,) in conn.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'ws_%'"
        ).fetchall():
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')
            conn.commit()  # one schema per transaction, or the lock table overflows


@pytest.fixture(autouse=True)
def _keep_database_schema(monkeypatch):
    """`grc-web demo` switches GRC_DATABASE_SCHEMA for its process; tests call it
    in-process, so put the suite's setting back after each test."""
    if "GRC_DATABASE_SCHEMA" in os.environ:
        monkeypatch.setenv("GRC_DATABASE_SCHEMA", os.environ["GRC_DATABASE_SCHEMA"])


@pytest.fixture(autouse=True)
def _api_mode(monkeypatch):
    """Tests control the AI mode themselves; a developer's .env must not switch it."""
    monkeypatch.setenv("GRC_AI_MODE", "api")
    monkeypatch.delenv("GRC_AI_PROVIDER", raising=False)
    for name in (
        "GRC_HTTPS",
        "GRC_SECURE_COOKIES",
        "GRC_FORCE_HTTPS",
        "GRC_TLS_CERT",
        "GRC_TLS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # No test may reach a real AI provider (evidence checks start on their own after an
    # upload when a key is set), so a developer's keys are hidden here too.
    for provider in PROVIDERS.values():
        if provider.key_env:
            monkeypatch.delenv(provider.key_env, raising=False)


@pytest.fixture(autouse=True)
def _no_firm_aws(monkeypatch):
    """Tests never call real AWS: by default this machine has no firm credentials."""

    def missing():
        raise ConnectorError("The firm's AWS credentials don't work (NoCredentials).")

    monkeypatch.setattr("grc_agent.connectors.cloud.firm_account_id", missing)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("GRC_SECRET_KEY", raising=False)
    monkeypatch.delenv("GRC_CORPUS_INDEX", raising=False)
    monkeypatch.setenv("GRC_CORPUS_DIR", str(tmp_path / "no-corpus"))
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert (
        web_cli.main(["--data-dir", str(tmp_path), "adduser", "harshit", "--password-stdin"]) == 0
    )
    return create_app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def authed(client):
    assert login(client).status_code == 303
    return client
