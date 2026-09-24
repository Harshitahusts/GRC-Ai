"""Shared fixtures: a fresh app with one user, and clients logged in or not."""

import io

import pytest
from fastapi.testclient import TestClient
from helpers import PASSWORD, login

from grc_agent.web import cli as web_cli
from grc_agent.web.app import create_app


@pytest.fixture(autouse=True)
def _api_mode(monkeypatch):
    """Tests control the AI mode themselves; a developer's .env must not switch it."""
    monkeypatch.setenv("GRC_AI_MODE", "api")


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
