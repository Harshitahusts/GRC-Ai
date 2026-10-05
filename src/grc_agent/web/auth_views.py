"""Ways in besides username and password: Google and Microsoft sign-in, emailed invites,
and password resets by email. Also the Account page, where people change their password
and connect or disconnect a Google or Microsoft account.

Accounts are still created only by an admin (Team & roles); there is no open sign-up.
A Google or Microsoft account signs someone in only after it has been connected to their
GRC Flow account, either from an invite link or from the Account page while signed in.
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import hashlib
import hmac
import html
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from grc_agent.web import db, mailer, oauth
from grc_agent.web.security import hash_password, verify_password

# Stored as the password hash of an account that has no password (invited, or Google /
# Microsoft only). verify_password() can never match it.
NO_PASSWORD = "!none"
MIN_PASSWORD = 10
INVITE_HOURS = 7 * 24
RESET_HOURS = 1
RESET_LIMIT, RESET_WINDOW = 3, 15 * 60  # reset emails per account or address per window


def has_password(row) -> bool:
    return not str(row["password_hash"]).startswith("!")


def app_url(request: Request) -> str:
    """The app's public address, for links in emails and the sign-in redirect URI.

    GRC_PUBLIC_URL, else https://GRC_DOMAIN, else the address of this request (local use)."""
    url = os.getenv("GRC_PUBLIC_URL", "").strip().rstrip("/")
    if url.startswith(("https://", "http://")):
        return url
    if domain := os.getenv("GRC_DOMAIN", "").strip():
        return f"https://{domain}"
    return str(request.base_url).rstrip("/")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _later(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def issue_token(conn, username: str, purpose: str, by: str) -> str:
    """A new single-use link token; earlier unused ones of the same kind stop working."""
    conn.execute(
        "UPDATE auth_tokens SET used_at = ? WHERE LOWER(username) = LOWER(?) AND purpose = ? "
        "AND used_at IS NULL",
        (db.now(), username, purpose),
    )
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_tokens (token_hash, username, purpose, created_by, created_at, "
        "expires_at) VALUES (?,?,?,?,?,?)",
        (
            _hash(token),
            username,
            purpose,
            by,
            db.now(),
            _later(INVITE_HOURS if purpose == "invite" else RESET_HOURS),
        ),
    )
    return token


def live_token(conn, token: str, purpose: str):
    """The token's row if it is unused, unexpired and its account still exists."""
    if not token or len(token) > 100:
        return None
    return conn.execute(
        "SELECT t.*, u.username AS account FROM auth_tokens t JOIN users u "
        "ON LOWER(u.username) = LOWER(t.username) "
        "WHERE t.token_hash = ? AND t.purpose = ? AND t.used_at IS NULL AND t.expires_at > ?",
        (_hash(token), purpose, db.now()),
    ).fetchone()


def send_invite(conn, request: Request, username: str, email: str, by: str) -> None:
    token = issue_token(conn, username, "invite", by)
    lines = [
        f"{by} has added you to GRC Flow, a workspace for DPDP Act readiness work.",
        f"Your username is {username}. Use the button to set a password"
        + (" or connect your Google or Microsoft account" if oauth.enabled() else "")
        + ". The link works once and expires in 7 days.",
    ]
    mailer.send(
        email,
        "You're invited to GRC Flow",
        lines,
        link=f"{app_url(request)}/invite/{token}",
        button="Accept the invite",
    )
    db.audit(conn, by, "invite_sent", None, {"username": username, "to": _mask(email)})


def send_reset(conn, request: Request, username: str, email: str, by: str) -> None:
    token = issue_token(conn, username, "reset", by)
    mailer.send(
        email,
        "Reset your GRC Flow password",
        [
            f"Someone asked to reset the password of the GRC Flow account {username}.",
            "If it was you, use the button within an hour. If not, ignore this email: "
            "nothing changes until the link is used.",
        ],
        link=f"{app_url(request)}/reset/{token}",
        button="Set a new password",
    )
    db.audit(conn, by, "reset_link_sent", None, {"username": username, "to": _mask(email)})


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}…@{domain}" if domain else "…"


def password_problem(password: str, confirm: str) -> str:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > 256:
        return "That password is too long."
    if password != confirm:
        return "The two passwords don't match."
    return ""


def _bounce(target: str) -> HTMLResponse:
    """A same-site hop. The session cookie is SameSite=Strict, so the browser leaves it
    off the redirect that comes back from Google or Microsoft; from this page it's sent."""
    safe = html.escape(target, quote=True)
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={safe}">'
        f'<title>Signing in…</title><p>Signing you in… <a href="{safe}">Continue</a></p>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def register(app: FastAPI) -> None:
    from grc_agent.web.app import (
        Conn,
        User,
        flash,
        form_with_csrf,
        public_demo,
        redirect,
        render,
        session_stamp,
        start_session,
    )

    app.state.reset_requests = {}

    def sign_in(request: Request, conn, username: str, via: str):
        row = conn.execute(
            "SELECT username, password_hash FROM users WHERE LOWER(username) = LOWER(?)",
            (username,),
        ).fetchone()
        start_session(request, row["username"], row["password_hash"])
        request.app.state.login_failures.pop(username.lower(), None)
        db.audit(conn, username, "login", None, {"via": via})
        return redirect("/")

    def login_error(request: Request, message: str, status: int = 400):
        return render(request, "login.html", status_code=status, error=message)

    def no_demo():
        if public_demo():
            raise HTTPException(status_code=404, detail="Not available in the public demo.")

    # ---- forgot password

    @app.get("/forgot")
    def forgot_page(request: Request):
        no_demo()
        return render(request, "forgot.html", mail_on=mailer.configured())

    @app.post("/forgot")
    async def forgot(request: Request, conn: Conn):
        no_demo()
        form = await form_with_csrf(request)
        who = str(form.get("who", "")).strip()[:254]
        sent = mailer.configured() and bool(who)
        if sent:
            stamps = request.app.state.reset_requests
            if len(stamps) > 5000:  # forget old entries rather than grow without bound
                stamps.clear()
            key = who.lower()
            recent = [t for t in stamps.get(key, []) if time.monotonic() - t < RESET_WINDOW]
            row = conn.execute(
                "SELECT username, email FROM users WHERE LOWER(username) = LOWER(?) "
                "OR (email <> '' AND LOWER(email) = LOWER(?))",
                (who, who),
            ).fetchone()
            if row and row["email"] and len(recent) < RESET_LIMIT:
                stamps[key] = [*recent, time.monotonic()]
                try:
                    send_reset(conn, request, row["username"], row["email"], row["username"])
                except mailer.MailError as exc:
                    db.audit(conn, row["username"], "email_failed", None, {"error": str(exc)})
        # The same answer whether or not the account exists.
        return render(request, "forgot.html", mail_on=mailer.configured(), sent=sent)

    # ---- set a password from an emailed link (reset or invite)

    @app.get("/reset/{token}")
    def reset_page(token: str, request: Request, conn: Conn):
        no_demo()
        row = live_token(conn, token, "reset")
        if row is None:
            return render(request, "error.html", status_code=410, message=EXPIRED)
        return render(request, "set_password.html", mode="reset", account=row["account"])

    @app.get("/invite/{token}")
    def invite_page(token: str, request: Request, conn: Conn):
        no_demo()
        row = live_token(conn, token, "invite")
        if row is None:
            return render(request, "error.html", status_code=410, message=EXPIRED)
        return render(
            request,
            "set_password.html",
            mode="invite",
            account=row["account"],
            token=token,
            providers=oauth.enabled(),
        )

    async def set_from_link(token: str, request: Request, conn, purpose: str):
        no_demo()
        form = await form_with_csrf(request)
        row = live_token(conn, token, purpose)
        if row is None:
            return render(request, "error.html", status_code=410, message=EXPIRED)
        password = str(form.get("password", ""))
        problem = password_problem(password, str(form.get("confirm", "")))
        if problem:
            return render(
                request,
                "set_password.html",
                status_code=400,
                mode=purpose,
                account=row["account"],
                token=token,
                providers=oauth.enabled() if purpose == "invite" else [],
                error=problem,
            )
        conn.execute("UPDATE auth_tokens SET used_at = ? WHERE id = ?", (db.now(), row["id"]))
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE LOWER(username) = LOWER(?)",
            (hash_password(password), row["account"]),
        )
        db.audit(
            conn,
            row["account"],
            "invite_accepted" if purpose == "invite" else "password_reset",
            None,
            {"via": "password"},
        )
        return sign_in(request, conn, row["account"], "emailed link")

    @app.post("/reset/{token}")
    async def reset_set(token: str, request: Request, conn: Conn):
        return await set_from_link(token, request, conn, "reset")

    @app.post("/invite/{token}")
    async def invite_set(token: str, request: Request, conn: Conn):
        return await set_from_link(token, request, conn, "invite")

    # ---- Google / Microsoft

    def provider_or_404(key: str) -> oauth.Provider:
        no_demo()
        provider = oauth.PROVIDERS.get(key)
        if provider is None or not provider.configured:
            raise HTTPException(status_code=404, detail="That sign-in option isn't set up.")
        return provider

    def start(request: Request, provider: oauth.Provider, intent: str, **extra: str):
        flow = oauth.new_flow()
        request.session["oauth"] = {"provider": provider.key, "intent": intent, **flow, **extra}
        redirect_uri = f"{app_url(request)}/auth/{provider.key}/callback"
        return redirect(oauth.authorize_url(provider, redirect_uri, flow))

    @app.get("/auth/{key}")
    def oauth_start(key: str, request: Request, intent: str = "login"):
        provider = provider_or_404(key)
        if intent == "link":
            user = request.session.get("user")
            if not user:
                return redirect("/login")
            return start(request, provider, "link", user=user)
        return start(request, provider, "login")

    @app.get("/invite/{token}/sso/{key}")
    def invite_sso(token: str, key: str, request: Request, conn: Conn):
        provider = provider_or_404(key)
        if live_token(conn, token, "invite") is None:
            return render(request, "error.html", status_code=410, message=EXPIRED)
        return start(request, provider, "invite", token_hash=_hash(token))

    @app.get("/auth/{key}/callback")
    def oauth_callback(key: str, request: Request):
        provider_or_404(key)
        params = request.query_params
        keep = {k: params[k] for k in ("code", "state", "error") if k in params}
        return _bounce(f"/auth/{key}/finish?{urllib.parse.urlencode(keep)}")

    @app.get("/auth/{key}/finish")
    async def oauth_finish(key: str, request: Request, conn: Conn):
        provider = provider_or_404(key)
        flow = request.session.pop("oauth", None) or {}
        state = request.query_params.get("state", "")
        if (
            flow.get("provider") != key
            or not state
            or not hmac.compare_digest(str(flow.get("state", "")), state)
        ):
            return login_error(request, "That sign-in link has expired. Try again.")
        if request.query_params.get("error") or not request.query_params.get("code"):
            return login_error(request, f"{provider.label} sign-in was cancelled.")
        try:
            who = await run_in_threadpool(
                oauth.identity_from_code,
                provider,
                request.query_params["code"],
                f"{app_url(request)}/auth/{key}/callback",
                flow["verifier"],
            )
        except oauth.OAuthError as exc:
            return login_error(request, str(exc), 502)

        linked = conn.execute(
            "SELECT i.*, u.username AS account FROM login_identities i LEFT JOIN users u "
            "ON LOWER(u.username) = LOWER(i.username) WHERE i.provider = ? AND i.subject = ?",
            (who.provider, who.subject),
        ).fetchone()
        intent = flow.get("intent")

        if intent == "login":
            if linked is None or linked["account"] is None:
                db.audit(conn, "-", "login_failed", None, {"via": key, "reason": "not connected"})
                return login_error(
                    request,
                    f"This {provider.label} account isn't connected to GRC Flow yet. Sign in "
                    "with your password and connect it under Account, or ask an admin for an "
                    "invite.",
                    403,
                )
            conn.execute(
                "UPDATE login_identities SET last_used_at = ?, email = ? WHERE id = ?",
                (db.now(), who.email, linked["id"]),
            )
            return sign_in(request, conn, linked["account"], key)

        if intent == "link":
            user = request.session.get("user")
            if not user or user != flow.get("user"):
                return login_error(request, "Sign in first, then connect the account.")
            if linked is not None and linked["account"] is not None:
                same = linked["account"].lower() == user.lower()
                flash(
                    request,
                    f"That {provider.label} account is already connected"
                    + (" to you." if same else " to another GRC Flow account."),
                    "info" if same else "error",
                )
                return redirect("/account")
            link_identity(conn, user, who)
            flash(request, f"Connected your {provider.label} account ({who.email or 'no email'}).")
            return redirect("/account")

        if intent == "invite":
            row = conn.execute(
                "SELECT t.*, u.username AS account FROM auth_tokens t JOIN users u "
                "ON LOWER(u.username) = LOWER(t.username) WHERE t.token_hash = ? "
                "AND t.purpose = 'invite' AND t.used_at IS NULL AND t.expires_at > ?",
                (str(flow.get("token_hash", "")), db.now()),
            ).fetchone()
            if row is None:
                return render(request, "error.html", status_code=410, message=EXPIRED)
            if linked is not None and linked["account"] is not None:
                if linked["account"].lower() != row["account"].lower():
                    return login_error(
                        request,
                        f"That {provider.label} account is already connected to another "
                        "GRC Flow account.",
                        409,
                    )
            else:
                link_identity(conn, row["account"], who)
            conn.execute("UPDATE auth_tokens SET used_at = ? WHERE id = ?", (db.now(), row["id"]))
            db.audit(conn, row["account"], "invite_accepted", None, {"via": key})
            return sign_in(request, conn, row["account"], key)

        return login_error(request, "That sign-in link has expired. Try again.")

    def link_identity(conn, username: str, who: oauth.Identity) -> None:
        conn.execute(
            "DELETE FROM login_identities WHERE provider = ? AND subject = ?",
            (who.provider, who.subject),
        )  # an old link left by a deleted account
        conn.execute(
            "INSERT INTO login_identities (username, provider, subject, email, created_at, "
            "last_used_at) VALUES (?,?,?,?,?,?)",
            (username, who.provider, who.subject, who.email, db.now(), db.now()),
        )
        db.audit(conn, username, "sign_in_connected", None, {"provider": who.provider})

    # ---- Account page

    @app.get("/account")
    def account_page(request: Request, user: User, conn: Conn):
        me = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (user,)
        ).fetchone()
        identities = conn.execute(
            "SELECT * FROM login_identities WHERE LOWER(username) = LOWER(?) ORDER BY id",
            (user,),
        ).fetchall()
        return render(
            request,
            "account.html",
            me=me,
            has_password=has_password(me),
            identities=identities,
            providers=oauth.enabled(),
            provider_labels={k: p.label for k, p in oauth.PROVIDERS.items()},
            mail_on=mailer.configured(),
        )

    @app.post("/account/email")
    async def account_email(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        email = str(form.get("email", "")).strip()
        problem = email_problem(conn, email, user)
        if problem:
            flash(request, problem, "error")
            return redirect("/account")
        conn.execute("UPDATE users SET email = ? WHERE LOWER(username) = LOWER(?)", (email, user))
        db.audit(conn, user, "email_changed", None, {"set": bool(email)})
        flash(request, "Email saved." if email else "Email removed.")
        return redirect("/account")

    @app.post("/account/password")
    async def account_password(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        me = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (user,)
        ).fetchone()
        if has_password(me) and not verify_password(
            str(form.get("current", "")), me["password_hash"]
        ):
            flash(request, "Your current password isn't right.", "error")
            return redirect("/account")
        password = str(form.get("password", ""))
        problem = password_problem(password, str(form.get("confirm", "")))
        if problem:
            flash(request, problem, "error")
            return redirect("/account")
        new_hash = hash_password(password)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE LOWER(username) = LOWER(?)",
            (new_hash, user),
        )
        # Every other session (another browser, a stolen cookie) is signed out; this one stays.
        request.session["pv"] = session_stamp(new_hash)
        db.audit(conn, user, "password_changed")
        flash(request, "Password changed.")
        return redirect("/account")

    @app.post("/account/sign-ins/{iid}/remove")
    async def account_unlink(iid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = conn.execute(
            "SELECT * FROM login_identities WHERE id = ? AND LOWER(username) = LOWER(?)",
            (iid, user),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such sign-in.")
        me = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (user,)
        ).fetchone()
        others = conn.execute(
            "SELECT COUNT(*) FROM login_identities WHERE LOWER(username) = LOWER(?) AND id <> ?",
            (user, iid),
        ).fetchone()[0]
        if not has_password(me) and not others:
            flash(
                request,
                "Set a password first, or you'd have no way to sign in.",
                "error",
            )
            return redirect("/account")
        conn.execute("DELETE FROM login_identities WHERE id = ?", (iid,))
        db.audit(conn, user, "sign_in_disconnected", None, {"provider": row["provider"]})
        provider = oauth.PROVIDERS.get(row["provider"])
        flash(request, f"Disconnected {provider.label if provider else row['provider']}.")
        return redirect("/account")


EXPIRED = (
    "This link has expired or was already used. Ask an admin to send a new one, or use "
    "“Forgot password?” on the sign-in page."
)


def email_problem(conn, email: str, username: str) -> str:
    """Why an email can't be saved for this account, or '' (an empty email is fine)."""
    if not email:
        return ""
    if not mailer.valid_email(email):
        return "That email address doesn't look right."
    taken = conn.execute(
        "SELECT username FROM users WHERE email <> '' AND LOWER(email) = LOWER(?) "
        "AND LOWER(username) <> LOWER(?)",
        (email, username),
    ).fetchone()
    return "Another account already uses that email." if taken else ""
