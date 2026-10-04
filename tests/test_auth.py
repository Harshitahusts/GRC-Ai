"""Invites, password resets by email, the Account page, and Google / Microsoft sign-in."""

import html
import re
import urllib.parse

import pytest
from fastapi.testclient import TestClient
from helpers import PASSWORD, csrf, login, post

from grc_agent.connectors.base import Response
from grc_agent.web import auth_views, db, mailer, oauth

NEW_PASSWORD = "a-brand-new-password"


@pytest.fixture
def outbox(monkeypatch):
    """Email is set up, and every email lands here instead of going to Resend."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("GRC_MAIL_FROM", "GRC Flow <noreply@example.com>")
    monkeypatch.setenv("GRC_PUBLIC_URL", "https://app.example.com")
    sent = []

    def fake_send(to, subject, lines, link="", button=""):
        sent.append({"to": to, "subject": subject, "lines": lines, "link": link})

    monkeypatch.setattr(mailer, "send", fake_send)
    return sent


@pytest.fixture
def google(monkeypatch):
    """Google sign-in is set up; the person who signs in is whoever `who` says."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GRC_PUBLIC_URL", "https://app.example.com")
    who = {"subject": "g-123", "email": "harshit@example.com"}
    seen = {}

    def fake_identity(provider, code, redirect_uri, verifier):
        seen.update(code=code, redirect_uri=redirect_uri, verifier=verifier)
        return oauth.Identity(provider.key, who["subject"], who["email"], "Harshit")

    monkeypatch.setattr(oauth, "identity_from_code", fake_identity)
    return who, seen


def text(r):
    return html.unescape(r.text)


def path_of(link):
    return urllib.parse.urlsplit(link).path


def conn(app):
    return db.connect(app.state.db_path)


def sso(client, start="/auth/google", code="the-code"):
    """Go through a Google sign-in from `start`; return the final response."""
    r = client.get(start, follow_redirects=False)
    assert r.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://app.example.com/auth/google/callback"]
    state = query["state"][0]
    bounce = client.get(f"/auth/google/callback?code={code}&state={state}")
    finish = re.search(r'url=([^"]+)"', bounce.text).group(1).replace("&amp;", "&")
    assert finish.startswith("/auth/google/finish?")
    return client.get(finish)


# ---- invites


def test_invite_by_email_then_set_a_password(app, authed, outbox):
    r = post(authed, "/team", {"username": "priya", "email": "priya@example.com", "role": "member"})
    assert "emailed an invite to priya@example.com" in text(r)
    (mail,) = outbox
    assert mail["to"] == "priya@example.com" and mail["link"].startswith(
        "https://app.example.com/invite/"
    )
    with conn(app) as c:
        row = c.execute("SELECT * FROM users WHERE username = 'priya'").fetchone()
        assert not auth_views.has_password(row)
        stored = c.execute("SELECT token_hash FROM auth_tokens").fetchone()[0]
    assert mail["link"].rsplit("/", 1)[1] not in stored  # only a hash is kept

    guest = TestClient(app)
    page = guest.get(path_of(mail["link"]))
    assert "Your username is <strong>priya</strong>" in text(page)
    token = csrf(guest, path_of(mail["link"]))
    short = guest.post(
        path_of(mail["link"]), data={"csrf": token, "password": "short", "confirm": "short"}
    )
    assert short.status_code == 400 and "at least 10" in text(short)
    done = guest.post(
        path_of(mail["link"]),
        data={"csrf": token, "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert done.url.path == "/" and "priya" in text(done)
    # The link works once.
    assert TestClient(app).get(path_of(mail["link"])).status_code == 410


def test_invited_account_cannot_sign_in_with_any_password(app, authed, outbox):
    post(authed, "/team", {"username": "priya", "email": "priya@example.com", "role": "member"})
    guest = TestClient(app)
    for password in ("", "!none", "none"):
        r = guest.post(
            "/login", data={"username": "priya", "password": password, "csrf": csrf(guest)}
        )
        assert r.status_code == 401


def test_invite_needs_email_set_up(authed):
    r = post(authed, "/team", {"username": "priya", "email": "priya@example.com", "role": "member"})
    assert "Give a starting password" in text(r) and "isn't set up" in text(r)


def test_password_account_still_works_without_email(authed):
    r = post(authed, "/team", {"username": "dev", "password": "long-enough-pw", "role": "viewer"})
    assert "Share the starting password privately" in text(r)


def test_admin_resends_invite_or_reset(app, authed, outbox):
    post(authed, "/team", {"username": "priya", "email": "priya@example.com", "role": "member"})
    post(authed, "/team/priya/send-link")
    assert len(outbox) == 2 and "/invite/" in outbox[1]["link"]
    # The first invite link no longer works once a new one is sent.
    assert TestClient(app).get(path_of(outbox[0]["link"])).status_code == 410
    post(authed, "/team/harshit/email", {"email": "harshit@example.com"})
    post(authed, "/team/harshit/send-link")
    assert "/reset/" in outbox[2]["link"]


def test_emails_are_unique_and_checked(authed, outbox):
    post(authed, "/team/harshit/email", {"email": "harshit@example.com"})
    r = post(authed, "/team", {"username": "two", "email": "HARSHIT@example.com"})
    assert "Another account already uses that email" in text(r)
    r = post(authed, "/team/harshit/email", {"email": "not-an-email"})
    assert "doesn't look right" in text(r)


# ---- forgot password


def test_forgot_password_sends_a_one_hour_link(app, client, authed, outbox):
    post(authed, "/team/harshit/email", {"email": "harshit@example.com"})
    guest = TestClient(app)
    r = guest.post("/forgot", data={"who": "harshit@example.com", "csrf": csrf(guest, "/forgot")})
    assert "a link to set a new password is on its way" in text(r)
    (mail,) = outbox
    link = path_of(mail["link"])
    token = csrf(guest, link)
    r = guest.post(link, data={"csrf": token, "password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert r.url.path == "/"
    assert login(TestClient(app)).status_code == 401  # the old password is gone
    assert login(TestClient(app), NEW_PASSWORD).status_code == 303
    assert TestClient(app).get(link).status_code == 410


def test_forgot_password_says_the_same_for_unknown_accounts(app, outbox):
    guest = TestClient(app)
    for who in ("nobody", "harshit"):  # harshit has no email yet
        r = guest.post("/forgot", data={"who": who, "csrf": csrf(guest, "/forgot")})
        assert "is on its way" in text(r)
    assert outbox == []


def test_forgot_password_is_rate_limited(app, authed, outbox):
    post(authed, "/team/harshit/email", {"email": "harshit@example.com"})
    guest = TestClient(app)
    for _ in range(5):
        guest.post("/forgot", data={"who": "harshit", "csrf": csrf(guest, "/forgot")})
    assert len(outbox) == auth_views.RESET_LIMIT


def test_expired_reset_link(app, authed, outbox):
    post(authed, "/team/harshit/email", {"email": "harshit@example.com"})
    post(authed, "/team/harshit/send-link")
    with conn(app) as c:
        c.execute("UPDATE auth_tokens SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert TestClient(app).get(path_of(outbox[0]["link"])).status_code == 410


def test_forgot_page_without_email_says_ask_an_admin(client):
    assert "ask an admin" in client.get("/forgot").text


# ---- account page


def test_change_password_needs_the_current_one(app, authed):
    r = post(
        authed,
        "/account/password",
        {"current": "wrong", "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert "current password isn't right" in text(r)
    r = post(
        authed,
        "/account/password",
        {"current": PASSWORD, "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert "Password changed" in text(r)
    assert login(TestClient(app), NEW_PASSWORD).status_code == 303


def test_viewer_can_change_own_password(app, authed):
    post(authed, "/team", {"username": "vic", "password": "viewer-password", "role": "viewer"})
    viewer = TestClient(app)
    viewer.post(
        "/login",
        data={"username": "vic", "password": "viewer-password", "csrf": csrf(viewer)},
    )
    r = post(
        viewer,
        "/account/password",
        {"current": "viewer-password", "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert "Password changed" in text(r)


# ---- Google / Microsoft


def test_sign_in_buttons_show_only_when_set_up(client, google):
    page = client.get("/login").text
    assert 'href="/auth/google"' in page and "Continue with Google" in page
    assert "Continue with Microsoft" not in page
    assert client.get("/auth/microsoft", follow_redirects=False).status_code == 404


def test_unconnected_google_account_is_refused(client, google):
    r = sso(client)
    assert r.status_code == 403 and "isn't connected to GRC Flow yet" in text(r)
    assert client.get("/", follow_redirects=False).status_code == 303  # still signed out


def test_connect_then_sign_in_with_google(app, authed, google):
    who, seen = google
    r = sso(authed, "/auth/google?intent=link")
    assert "Connected your Google account" in text(r)
    assert seen["redirect_uri"] == "https://app.example.com/auth/google/callback"
    assert len(seen["verifier"]) >= 43
    post(authed, "/logout")
    fresh = TestClient(app)
    r = sso(fresh)
    assert r.url.path == "/" and "harshit" in text(r)
    with conn(app) as c:
        actions = [row[0] for row in c.execute("SELECT action FROM audit_log")]
    assert "sign_in_connected" in actions


def test_matching_email_alone_never_signs_anyone_in(app, authed, google):
    post(authed, "/team/harshit/email", {"email": "harshit@example.com"})
    r = sso(TestClient(app))  # same email as harshit's, but never connected
    assert r.status_code == 403


def test_state_must_match(client, google):
    client.get("/auth/google", follow_redirects=False)
    r = client.get("/auth/google/finish?code=x&state=forged")
    assert "expired" in text(r) and r.status_code == 400
    assert client.get("/auth/google/finish?code=x&state=").status_code == 400


def test_cancelled_sign_in(client, google):
    r = client.get("/auth/google", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["state"][0]
    r = client.get(f"/auth/google/finish?error=access_denied&state={state}")
    assert "cancelled" in text(r)


def test_one_google_account_one_grc_account(app, authed, google, outbox):
    sso(authed, "/auth/google?intent=link")
    post(authed, "/team", {"username": "priya", "email": "priya@example.com", "role": "member"})
    invite = path_of(outbox[0]["link"])
    guest = TestClient(app)
    r = sso(guest, f"{invite}/sso/google")
    assert r.status_code == 409 and "another GRC Flow account" in text(r)


def test_accept_invite_with_google(app, authed, google, outbox):
    who, _ = google
    who.update(subject="g-priya", email="priya@gmail.com")
    post(authed, "/team", {"username": "priya", "email": "priya@example.com", "role": "member"})
    invite = path_of(outbox[0]["link"])
    guest = TestClient(app)
    assert "Continue with Google" in guest.get(invite).text
    r = sso(guest, f"{invite}/sso/google")
    assert r.url.path == "/" and "priya" in text(r)
    assert TestClient(app).get(invite).status_code == 410
    # From now on Google signs priya in directly.
    assert sso(TestClient(app)).url.path == "/"


def test_cannot_disconnect_the_only_way_in(app, authed, google, outbox):
    who, _ = google
    who.update(subject="g-priya")
    post(authed, "/team", {"username": "priya", "email": "priya@example.com", "role": "member"})
    guest = TestClient(app)
    sso(guest, f"{path_of(outbox[0]['link'])}/sso/google")
    with conn(app) as c:
        iid = c.execute("SELECT id FROM login_identities").fetchone()[0]
    r = post(guest, f"/account/sign-ins/{iid}/remove")
    assert "Set a password first" in text(r)
    r = post(guest, "/account/password", {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert "Password changed" in text(r)  # no current password needed: there wasn't one
    r = post(guest, f"/account/sign-ins/{iid}/remove")
    assert "Disconnected Google" in text(r)


def test_someone_elses_sign_in_cannot_be_removed(app, authed, google):
    sso(authed, "/auth/google?intent=link")
    with conn(app) as c:
        iid = c.execute("SELECT id FROM login_identities").fetchone()[0]
    post(authed, "/team", {"username": "dev", "password": "long-enough-pw", "role": "member"})
    other = TestClient(app)
    other.post(
        "/login", data={"username": "dev", "password": "long-enough-pw", "csrf": csrf(other)}
    )
    assert post(other, f"/account/sign-ins/{iid}/remove").status_code == 404


def test_public_demo_has_no_sso_or_resets(client, google, monkeypatch):
    monkeypatch.setenv("GRC_PUBLIC_DEMO", "1")
    assert client.get("/auth/google", follow_redirects=False).status_code == 404
    assert client.get("/forgot").status_code == 404
    assert "Continue with Google" not in client.get("/login").text


# ---- the pieces


def test_authorize_url_carries_pkce_and_state(monkeypatch):
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "mid")
    monkeypatch.setenv("MICROSOFT_TENANT", "contoso.onmicrosoft.com")
    flow = oauth.new_flow()
    url = oauth.authorize_url(oauth.PROVIDERS["microsoft"], "https://app/cb", flow)
    assert url.startswith(
        "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/authorize?"
    )
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["state"] == [flow["state"]] and flow["verifier"] not in url
    assert q["scope"] == ["openid email profile"]


def test_identity_from_code_uses_userinfo(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    calls = []

    def fake_request(method, url, headers=None, json_body=None, form=None):
        calls.append((method, url, form, headers))
        if "token" in url:
            return Response(200, {"access_token": "at"})
        return Response(200, {"sub": "42", "email": "a@b.co"})

    monkeypatch.setattr(oauth, "request", fake_request)
    who = oauth.identity_from_code(oauth.PROVIDERS["google"], "c", "https://app/cb", "v" * 50)
    assert who.subject == "42" and who.email == "a@b.co"
    assert calls[0][2]["code_verifier"] == "v" * 50
    assert calls[1][3]["Authorization"] == "Bearer at"


def test_identity_from_code_rejects_a_refused_code(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    resp = Response
    monkeypatch.setattr(oauth, "request", lambda *a, **k: resp(400, {"error": "invalid_grant"}))
    with pytest.raises(oauth.OAuthError):
        oauth.identity_from_code(oauth.PROVIDERS["google"], "c", "https://app/cb", "v")


def test_mail_is_sent_through_resend_escaped(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_key")
    monkeypatch.setenv("GRC_MAIL_FROM", "GRC Flow <noreply@example.com>")
    sent = {}
    resp = Response

    def fake_request(method, url, headers=None, json_body=None, form=None):
        sent.update(url=url, headers=headers, body=json_body)
        return resp(200, {"id": "1"})

    monkeypatch.setattr(mailer, "request", fake_request)
    mailer.send("a@example.com", "Hi", ["<b>Ravi</b> added you"], "https://x/y?a=1&b=2", "Go")
    assert sent["url"] == "https://api.resend.com/emails"
    assert sent["headers"]["Authorization"] == "Bearer re_key"
    assert "&lt;b&gt;Ravi&lt;/b&gt;" in sent["body"]["html"]
    assert "a=1&amp;b=2" in sent["body"]["html"]
    assert sent["body"]["to"] == ["a@example.com"]


def test_mail_refuses_bad_addresses_and_missing_setup(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(mailer.MailError):
        mailer.send("a@example.com", "Hi", ["x"])
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("GRC_MAIL_FROM", "x@example.com")
    with pytest.raises(mailer.MailError):
        mailer.send("bad\nBcc: evil@example.com", "Hi", ["x"])
