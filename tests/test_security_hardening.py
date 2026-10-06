"""Fixes from the October 2026 security review: sessions, login throttling, SSRF."""

import urllib.request

import pytest
from fastapi.testclient import TestClient
from helpers import PASSWORD, csrf, login, post

from grc_agent.connectors import ConnectorError, base
from grc_agent.web import db, https
from grc_agent.web.app import MAX_IP_FAILURES


def test_removed_user_loses_their_session(app, authed):
    assert authed.get("/", follow_redirects=False).status_code == 200
    with db.connect(app.state.db_path) as conn:
        conn.execute("DELETE FROM users WHERE username = 'harshit'")
        conn.commit()
    r = authed.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_password_change_signs_out_other_sessions(app, authed):
    other = TestClient(app)
    assert login(other).status_code == 303  # a second browser, or a stolen cookie
    r = post(
        authed,
        "/account/password",
        {
            "current": PASSWORD,
            "password": "a-brand-new-passphrase",
            "confirm": "a-brand-new-passphrase",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert authed.get("/", follow_redirects=False).status_code == 200  # this one stays
    assert other.get("/", follow_redirects=False).headers["location"] == "/login"


def test_failed_logins_are_limited_per_address(client):
    # Spraying: a few guesses at many usernames, each under its own lockout limit.
    for i in range(MAX_IP_FAILURES):
        client.post(
            "/login",
            data={"username": f"user{i}", "password": "Password1", "csrf": csrf(client)},
        )
    r = login(client)  # even the right password, from that address, waits
    assert r.status_code == 429


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/opc/v2", "http://[fe80::1]:11434", "http://metadata.google.internal"],
)
def test_ai_address_never_reaches_cloud_metadata(url):
    assert "metadata" in https.insecure_url_problem(url)


def test_ai_address_on_the_servers_network_only_when_allowed(monkeypatch):
    local = "http://127.0.0.1:11434/v1"
    assert https.insecure_url_problem(local) is None  # on a laptop: Ollama is fine
    monkeypatch.setenv("GRC_FORCE_HTTPS", "1")  # served to the internet
    assert "own network" in https.insecure_url_problem(local)
    assert "own network" in https.insecure_url_problem("http://10.0.0.5:8000/v1")
    monkeypatch.setenv("GRC_ALLOW_PRIVATE_AI_URL", "1")
    assert https.insecure_url_problem(local) is None


def test_connector_redirect_into_private_network_is_refused():
    handler = base._CheckedRedirects()
    req = urllib.request.Request("https://gitlab.example.com/api/v4/projects")
    for target in ("http://169.254.169.254/latest/meta-data/", "https://127.0.0.1/admin"):
        with pytest.raises(ConnectorError):
            handler.redirect_request(req, None, 302, "Found", {}, target)


def test_passwords_are_argon2id_and_old_scrypt_hashes_still_work(app, client):
    from grc_agent.web.security import hash_password, needs_rehash, verify_password

    new = hash_password("a long passphrase")
    assert new.startswith("$argon2id$") and verify_password("a long passphrase", new)
    assert not verify_password("wrong", new) and not needs_rehash(new)
    assert not verify_password("anything", "!none") and not needs_rehash("!none")

    # A hash made by the previous version (scrypt) signs in, and is upgraded on the way.
    import hashlib

    salt = bytes(16)
    digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=2**14, r=8, p=1)
    old = f"scrypt${2**14}$8$1${salt.hex()}${digest.hex()}"
    with db.connect(app.state.db_path) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = 'harshit'", (old,))
        conn.commit()
    assert login(client).status_code == 303
    with db.connect(app.state.db_path) as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE username = 'harshit'")
        stored = stored.fetchone()[0]
    assert stored.startswith("$argon2id$") and verify_password(PASSWORD, stored)
