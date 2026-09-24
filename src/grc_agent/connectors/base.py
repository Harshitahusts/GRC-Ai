"""Shared pieces for connectors: check results, errors, and a small HTTPS client."""

from __future__ import annotations

import base64
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

TIMEOUT = 20

# Evidence is linked to provisions, not register rows, so it stays valid when the
# register is replaced. The app shows it against every obligation citing them.
SECURITY = ["Section 8(5)"]
TRANSFERS = ["Section 16(1)"]

STATUSES = ("pass", "fail", "warn", "info")


class ConnectorError(Exception):
    """A problem to show the user. Messages never include credentials."""


@dataclass
class Check:
    key: str
    title: str
    status: str  # pass | fail | warn | info
    detail: str
    provisions: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_rs256_jwt(claims: dict[str, Any], private_key_pem: str) -> str:
    """A JSON Web Token signed with an RSA private key (used by GitHub Apps and GCP)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(claims).encode())
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except (ValueError, TypeError):
        raise ConnectorError("The private key isn't a valid PEM key.") from None
    signature = key.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64url(signature)}"


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def check_public_https(url: str) -> None:
    """Reject non-HTTPS URLs and hosts that resolve to private or local addresses.

    Stops a user-entered URL (a self-hosted GitLab, say) from being used to reach
    services on the machine or network the app runs on.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConnectorError("The address must start with https://")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise ConnectorError(f"Can't find the host {parsed.hostname}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ConnectorError(f"{parsed.hostname} is a private or local address.")


def check_host(url: str, allowed_suffixes: tuple[str, ...], what: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        host == s.lstrip(".") or host.endswith(s) for s in allowed_suffixes
    ):
        raise ConnectorError(f"That doesn't look like a {what} URL.")


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
) -> Response:
    """HTTPS request returning status and parsed JSON (or text). HTTP errors are
    returned, not raised; network failures raise ConnectorError."""
    data = None
    headers = {"Accept": "application/json", "User-Agent": "grc-agent", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (https checked by callers)
            return Response(resp.status, _parse(resp.read()))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, _parse(exc.read()))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        host = urllib.parse.urlparse(url).hostname
        raise ConnectorError(f"Couldn't reach {host}: {getattr(exc, 'reason', exc)}") from None


def _parse(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def expect_ok(resp: Response, what: str) -> Any:
    if resp.status in (401, 403):
        raise ConnectorError(f"{what}: access denied. Check the credentials and their permissions.")
    if not resp.ok:
        raise ConnectorError(f"{what} failed (HTTP {resp.status}).")
    return resp.body


def location_check(provider: str, locations: dict[str, list[str]], in_india) -> Check:
    """Where data is stored: evidence for cross-border transfers (Section 16)."""
    outside = {loc: names for loc, names in locations.items() if not in_india(loc)}
    inside = {loc: names for loc, names in locations.items() if in_india(loc)}
    if not locations:
        return Check(
            "data_location",
            "Where data is stored",
            "info",
            f"No storage found in {provider} to check.",
            TRANSFERS,
        )
    if outside:
        detail = "Stored outside India: " + "; ".join(
            f"{loc} ({len(n)}: {', '.join(n[:3])}{'…' if len(n) > 3 else ''})"
            for loc, n in sorted(outside.items())
        )
        status = "warn"
    else:
        detail = "All storage found is in Indian regions: " + ", ".join(sorted(inside))
        status = "pass"
    return Check(
        "data_location",
        "Where data is stored",
        status,
        detail,
        TRANSFERS,
        {"outside_india": sorted(outside), "inside_india": sorted(inside)},
    )
