"""Sign in with Google or Microsoft (OpenID Connect, authorization code flow with PKCE).

Set in .env, per provider (a provider without both values doesn't show on the sign-in page):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET, MICROSOFT_TENANT (default "common")

The redirect URI to register with each provider is <app URL>/auth/<provider>/callback,
e.g. https://app.grc-flow.com/auth/google/callback.

The app talks to the provider's token and userinfo endpoints directly over HTTPS with its
client secret, so the identity it gets back comes from the provider itself. A person is
recognised by the provider's stable subject id, never by email address (an email claim
from a multi-tenant Microsoft sign-in can be set by whoever runs that tenant).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import urllib.parse
from dataclasses import dataclass

from grc_agent.connectors.base import ConnectorError, request


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    env: str  # prefix of the client id / secret variables

    @property
    def client_id(self) -> str:
        return os.getenv(f"{self.env}_CLIENT_ID", "").strip()

    @property
    def client_secret(self) -> str:
        return os.getenv(f"{self.env}_CLIENT_SECRET", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def endpoints(self) -> tuple[str, str, str]:
        """(authorize, token, userinfo) URLs."""
        if self.key == "google":
            return (
                "https://accounts.google.com/o/oauth2/v2/auth",
                "https://oauth2.googleapis.com/token",
                "https://openidconnect.googleapis.com/v1/userinfo",
            )
        tenant = urllib.parse.quote(os.getenv("MICROSOFT_TENANT", "").strip() or "common", safe="")
        base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        return f"{base}/authorize", f"{base}/token", "https://graph.microsoft.com/oidc/userinfo"


PROVIDERS = {
    "google": Provider("google", "Google", "GOOGLE"),
    "microsoft": Provider("microsoft", "Microsoft", "MICROSOFT"),
}


class OAuthError(Exception):
    """Sign-in failed. The message is safe to show."""


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    email: str
    name: str


def enabled() -> list[Provider]:
    return [p for p in PROVIDERS.values() if p.configured]


def new_flow() -> dict[str, str]:
    """State and PKCE verifier for one sign-in attempt (kept in the session)."""
    return {"state": secrets.token_urlsafe(32), "verifier": secrets.token_urlsafe(48)}


def authorize_url(provider: Provider, redirect_uri: str, flow: dict[str, str]) -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {
        "client_id": provider.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": flow["state"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{provider.endpoints()[0]}?{urllib.parse.urlencode(params)}"


def identity_from_code(provider: Provider, code: str, redirect_uri: str, verifier: str) -> Identity:
    _, token_url, userinfo_url = provider.endpoints()
    try:
        token = request(
            "POST",
            token_url,
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
                "code_verifier": verifier,
            },
        )
        access = token.body.get("access_token") if isinstance(token.body, dict) else None
        if not token.ok or not access:
            raise OAuthError(f"{provider.label} didn't accept the sign-in. Try again.")
        info = request("GET", userinfo_url, headers={"Authorization": f"Bearer {access}"})
    except ConnectorError as exc:
        raise OAuthError(str(exc)) from None
    body = info.body if isinstance(info.body, dict) else {}
    subject = str(body.get("sub") or "").strip()
    if not info.ok or not subject:
        raise OAuthError(f"{provider.label} didn't say who you are. Try again.")
    return Identity(
        provider=provider.key,
        subject=subject[:255],
        email=str(body.get("email") or body.get("preferred_username") or "")[:254],
        name=str(body.get("name") or "")[:120],
    )
