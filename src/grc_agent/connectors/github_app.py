"""GitHub connector through a GitHub App: the client authorises read-only access.

Instead of pasting a personal token, the client installs our GitHub App on their
organisation, choosing which repositories to share and approving read-only
permissions on GitHub's own screen. They can remove it at any time from their
GitHub settings.

Setting up the app itself is a one-time step for the firm, done with GitHub's
app manifest flow: one click on GitHub creates the app, and GitHub hands back its
ID and private key, which are stored encrypted in the data folder.

Auth: the app signs a short JWT (valid under 10 minutes) with its private key,
and exchanges it for an installation token (valid 1 hour) scoped to what the
client granted.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from grc_agent.connectors.base import Check, ConnectorError, expect_ok, request, sign_rs256_jwt
from grc_agent.connectors.secrets import SecretBox
from grc_agent.connectors.vcs import github_repo_checks

API = "https://api.github.com"
PERMISSIONS = {"metadata": "read", "administration": "read"}
CONFIG_FILE = "github_app.enc"


@dataclass(frozen=True)
class AppConfig:
    app_id: str
    slug: str
    pem: str
    html_url: str = ""
    owner: str = ""


def load_config(data_dir: Path, box: SecretBox) -> AppConfig | None:
    """The app from GRC_GITHUB_APP_* settings, or from the manifest flow's saved file."""
    app_id, slug = os.getenv("GRC_GITHUB_APP_ID"), os.getenv("GRC_GITHUB_APP_SLUG")
    key = os.getenv("GRC_GITHUB_APP_PRIVATE_KEY")
    if app_id and slug and key:
        pem = Path(key).read_text() if not key.lstrip().startswith("-----") else key
        return AppConfig(app_id, slug, pem)
    path = data_dir / CONFIG_FILE
    if not path.exists():
        return None
    try:
        return AppConfig(**box.open(path.read_text()))
    except (ValueError, TypeError):
        return None


def save_config(data_dir: Path, box: SecretBox, config: AppConfig) -> None:
    path = data_dir / CONFIG_FILE
    path.write_text(box.seal(asdict(config)))
    path.chmod(0o600)


def manifest(name: str, base_url: str, homepage: str) -> dict:
    """The app GitHub creates: read-only, no webhooks, installable by any account."""
    return {
        "name": name,
        "url": homepage,
        "hook_attributes": {"url": f"{homepage.rstrip('/')}/github/events", "active": False},
        "redirect_url": f"{base_url}/settings/github-app/callback",
        "setup_url": f"{base_url}/connectors/github/setup",
        "setup_on_update": True,
        "public": True,  # so clients' organisations can install it
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def convert_manifest(code: str) -> AppConfig:
    """Swap the one-time code GitHub returns (valid 1 hour) for the new app's credentials."""
    if not re.fullmatch(r"[A-Za-z0-9]{8,100}", code or ""):
        raise ConnectorError("GitHub returned an invalid code.")
    data = expect_ok(
        request("POST", f"{API}/app-manifests/{code}/conversions"), "Creating the GitHub App"
    )
    return AppConfig(
        app_id=str(data["id"]),
        slug=data["slug"],
        pem=data["pem"],
        html_url=data.get("html_url", ""),
        owner=(data.get("owner") or {}).get("login", ""),
    )


def app_jwt(config: AppConfig, now: int | None = None) -> str:
    now = now or int(time.time())
    # Backdated a minute for clock drift; GitHub rejects tokens valid for over 10 minutes.
    return sign_rs256_jwt({"iat": now - 60, "exp": now + 540, "iss": config.app_id}, config.pem)


def _as_app(config: AppConfig, method: str, path: str):
    return request(
        method,
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {app_jwt(config)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def install_url(config: AppConfig, state: str) -> str:
    return f"https://github.com/apps/{config.slug}/installations/new?state={state}"


def get_installation(config: AppConfig, installation_id: str) -> dict:
    if not str(installation_id).isdigit():
        raise ConnectorError("Invalid installation ID.")
    resp = _as_app(config, "GET", f"/app/installations/{installation_id}")
    if resp.status == 404:
        raise ConnectorError("That installation doesn't exist or was removed.")
    return expect_ok(resp, "Reading the GitHub installation")


def find_installation(config: AppConfig, account: str) -> dict:
    installs = expect_ok(
        _as_app(config, "GET", "/app/installations?per_page=100"), "Listing installations"
    )
    for inst in installs:
        if inst.get("account", {}).get("login", "").lower() == account.strip().lower():
            return inst
    raise ConnectorError(
        f"The app isn't installed on '{account}' yet. Send the client the install link first."
    )


def installation_token(config: AppConfig, installation_id: str) -> str:
    resp = _as_app(config, "POST", f"/app/installations/{installation_id}/access_tokens")
    if resp.status == 404:
        raise ConnectorError(
            "The client removed the app from GitHub. Ask them to install it again."
        )
    return expect_ok(resp, "Getting access from GitHub")["token"]


def describe(installation: dict) -> dict:
    """What we store about a connection: IDs and names only, no credentials."""
    account = installation.get("account", {})
    return {
        "installation_id": str(installation["id"]),
        "account": account.get("login", ""),
        "repository_selection": installation.get("repository_selection", ""),
    }


def collect(config: AppConfig, installation_id: str) -> list[Check]:
    token = installation_token(config, installation_id)
    body = expect_ok(
        request(
            "GET",
            f"{API}/installation/repositories?per_page=100",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        ),
        "Listing repositories",
    )
    return github_repo_checks(body.get("repositories", []), token)
