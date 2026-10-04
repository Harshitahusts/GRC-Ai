"""HTTPS: security headers, HSTS, redirects, Secure cookies, certificates, AI addresses."""

import re

import anthropic
import pytest
from cryptography import x509
from fastapi.testclient import TestClient
from helpers import post

from grc_agent.llm import PROVIDERS, OpenAICompatClient
from grc_agent.web import https
from grc_agent.web.app import create_app


def test_every_response_carries_security_headers(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert "strict-transport-security" not in r.headers  # plain HTTP: no HSTS
    assert "nosniff" in client.get("/static/style.css").headers["x-content-type-options"]


def test_hsts_and_secure_cookie_over_https(tmp_path, monkeypatch):
    monkeypatch.setenv("GRC_HTTPS", "1")
    secure = TestClient(create_app(tmp_path), base_url="https://grc.example.in")
    r = secure.get("/login")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    cookie = r.headers["set-cookie"].lower()
    assert "grc_session=" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_frame_ancestors_lets_named_sites_frame_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("GRC_FRAME_ANCESTORS", "https://grc-flow.com, https://www.grc-flow.com")
    r = TestClient(create_app(tmp_path)).get("/login")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors https://grc-flow.com https://www.grc-flow.com" in csp
    assert "'none'" not in csp.split("frame-ancestors")[1].split(";")[0]
    assert "x-frame-options" not in r.headers


def test_sign_in_links_to_the_website_when_set(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    assert "Back to" not in TestClient(app).get("/login").text
    monkeypatch.setenv("GRC_SITE_URL", "https://grc-flow.com/")
    page = TestClient(app).get("/login").text
    assert 'href="https://grc-flow.com">&larr; Back to grc-flow.com' in page


def test_static_links_change_when_the_files_change(tmp_path):
    page = TestClient(create_app(tmp_path)).get("/login").text
    version = re.search(r'/static/style\.css\?v=([0-9a-f]{10})"', page).group(1)
    assert f'/static/app.js?v={version}"' in page


def test_force_https_redirects_remote_plain_http(tmp_path, monkeypatch):
    monkeypatch.setenv("GRC_FORCE_HTTPS", "1")
    app = create_app(tmp_path)
    remote = TestClient(app, base_url="http://grc.example.in")
    r = remote.get("/login?next=/", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "https://grc.example.in/login?next=/"
    # On this machine or the office network, plain HTTP still works.
    assert TestClient(app, base_url="http://127.0.0.1").get("/login").status_code == 200


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("192.168.1.20", True),
        ("10.0.0.5", True),
        ("[::1]", True),
        ("ollama.local", True),
        ("8.8.8.8", False),
        ("api.groq.com", False),
    ],
)
def test_local_hosts(host, local):
    assert https.is_local_host(host) is local


def test_self_signed_certificate_is_made_once_and_covers_the_lan(tmp_path):
    cert, key = https.self_signed_cert(tmp_path / "tls", ["192.168.1.20"])
    parsed = x509.load_pem_x509_certificate(open(cert, "rb").read())
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert {str(i) for i in san.get_values_for_type(x509.IPAddress)} >= {
        "127.0.0.1",
        "192.168.1.20",
    }
    assert b"PRIVATE KEY" in open(key, "rb").read()
    first = open(cert, "rb").read()
    https.self_signed_cert(tmp_path / "tls", ["192.168.1.20"])
    assert open(cert, "rb").read() == first  # reused, so browsers only warn once
    https.self_signed_cert(tmp_path / "tls", ["192.168.1.99"])  # new address: re-made
    assert open(cert, "rb").read() != first
    assert len(https.fingerprint(cert).split(":")) == 32


def test_tls_files_from_environment(tmp_path, monkeypatch):
    assert https.tls_files(tmp_path, False, []) is None
    monkeypatch.setenv("GRC_TLS_CERT", str(tmp_path / "missing.pem"))
    with pytest.raises(SystemExit, match="Set both"):
        https.tls_files(tmp_path, False, [])
    monkeypatch.setenv("GRC_TLS_KEY", str(tmp_path / "missing.key"))
    with pytest.raises(SystemExit, match="not found"):
        https.tls_files(tmp_path, False, [])


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://llm.example.com/v1", True),
        ("http://localhost:11434/v1", True),
        ("http://192.168.1.50:1234/v1", True),
        ("http://llm.example.com/v1", False),
        ("ftp://llm.example.com", False),
    ],
)
def test_ai_addresses_must_be_https_outside_the_network(url, ok):
    assert (https.insecure_url_problem(url) is None) is ok


def test_ai_page_refuses_plain_http_to_the_internet(authed):
    r = post(
        authed, "/settings/ai", {"provider": "custom", "base_url": "http://llm.example.com/v1"}
    )
    assert "Use https://" in r.text


def test_client_never_sends_a_key_over_plain_http_to_the_internet():
    client = OpenAICompatClient(
        PROVIDERS["custom"], api_key="secret", base_url="http://llm.example.com/v1"
    )
    with pytest.raises(anthropic.CredentialsError, match="plain HTTP"):
        client.beta.messages.create(model="m", max_tokens=5, messages=[])
