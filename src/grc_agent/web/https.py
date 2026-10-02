"""HTTPS for the web app: certificates, security headers and redirects.

Three ways to run over HTTPS, from simplest to most robust:

1. `grc-web serve --https` (or `demo --lan --https`): the app makes its own
   certificate in <data dir>/tls/ and serves HTTPS itself. Browsers warn once,
   because no public authority signed it, but the traffic is encrypted. Good for
   sharing on an office network.
2. GRC_TLS_CERT and GRC_TLS_KEY: your own certificate files (for example from
   your company's certificate authority, or Let's Encrypt).
3. A reverse proxy (Caddy, nginx, a cloud load balancer) terminates HTTPS and
   forwards to the app. Set GRC_TRUSTED_PROXIES to the proxy's address so the
   app believes its "this was HTTPS" header, and GRC_FORCE_HTTPS=1 to redirect
   any plain-HTTP request.

Whenever HTTPS is on, the session cookie is marked Secure (never sent over plain
HTTP) and browsers are told to use HTTPS for this site from then on (HSTS).
"""

import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

# Pages only load scripts, styles and images from the app itself. Inline scripts and
# styles are still allowed because the templates use them; everything else is locked
# down: no plugins, no framing (clickjacking), forms post only here and to GitHub
# (the GitHub App setup), and fetch() talks only to this server.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self' https://github.com",
        "frame-ancestors 'none'",
    ]
)
HSTS = "max-age=15552000"  # 180 days; no includeSubDomains, so other sites on the domain are safe
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def https_enabled() -> bool:
    """True when users reach the app over HTTPS, so cookies must be Secure."""
    return (
        _on("GRC_HTTPS")
        or _on("GRC_SECURE_COOKIES")
        or _on("GRC_FORCE_HTTPS")
        or os.getenv("GRC_PUBLIC_URL", "").lower().startswith("https://")
    )


def is_local_host(host: str) -> bool:
    """This machine or a private network: where plain HTTP never crosses the internet."""
    host = (host or "").strip("[]").lower()
    if host in {"localhost", ""} or host.endswith((".local", ".localhost", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def install(app: FastAPI) -> None:
    """Security headers on every response; HTTPS redirect and HSTS when HTTPS is on."""
    force = _on("GRC_FORCE_HTTPS")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        secure = request.url.scheme == "https"
        if force and not secure and not is_local_host(request.url.hostname or ""):
            # 308 keeps the method and body, so a POST is retried as a POST.
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if secure:
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response


# ------------------------------------------------------------------ certificates


def tls_files(data_dir: Path, self_signed: bool, extra_hosts: list[str]) -> tuple[str, str] | None:
    """(cert, key) to serve HTTPS with, or None for plain HTTP.

    GRC_TLS_CERT/GRC_TLS_KEY win; otherwise --https makes a self-signed certificate.
    """
    cert, key = os.getenv("GRC_TLS_CERT", ""), os.getenv("GRC_TLS_KEY", "")
    if cert or key:
        if not (cert and key):
            raise SystemExit("Set both GRC_TLS_CERT and GRC_TLS_KEY.")
        for path in (cert, key):
            if not Path(path).is_file():
                raise SystemExit(f"Certificate file not found: {path}")
        return cert, key
    if self_signed:
        return self_signed_cert(data_dir / "tls", extra_hosts)
    return None


def self_signed_cert(folder: Path, extra_hosts: list[str]) -> tuple[str, str]:
    """A certificate for localhost, this computer's name and its network addresses.

    Re-used across restarts (so browsers only warn once), and re-made when it is
    about to expire or the computer got a new address.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    names = sorted(
        {"localhost", "127.0.0.1", socket.gethostname().lower(), *filter(None, extra_hosts)}
    )
    names = [n for n in names if n.isascii()]
    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    if cert_path.exists() and key_path.exists():
        existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        covered = {str(v) for v in san.get_values_for_type(x509.DNSName)} | {
            str(v) for v in san.get_values_for_type(x509.IPAddress)
        }
        fresh = existing.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) > dt.timedelta(
            days=30
        )
        if fresh and set(names) <= covered:
            return str(cert_path), str(key_path)

    key = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    for name in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            if name.isascii():  # a computer name with other characters can't be a DNS name
                alt.append(x509.DNSName(name.lower()))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GRC agent (self-signed)")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=397))  # browsers' maximum
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)
    except OSError:  # Windows: file permissions work differently
        pass
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_path), str(key_path)


def fingerprint(cert_path: str) -> str:
    """SHA-256 fingerprint, so people can check the certificate their browser shows."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def insecure_url_problem(url: str) -> str | None:
    """Why an AI provider address is unsafe for an API key, or None if it is fine."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "The address must start with https:// (or http:// for a server on your network)."
    if parts.scheme == "http" and not is_local_host(parts.hostname):
        return (
            "Use https:// for a server on the internet. Plain http:// would send your "
            "API key and client data unencrypted."
        )
    return None
