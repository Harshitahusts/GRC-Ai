"""Version control connectors: GitHub, GitLab, Bitbucket.

Read-only checks on the organisation's code hosting, as evidence for reasonable
security safeguards (Section 8(5)): public repositories that could expose
personal data or secrets, unprotected default branches, and secret scanning.
"""

from __future__ import annotations

import urllib.parse

from grc_agent.connectors.base import (
    SECURITY,
    Check,
    ConnectorError,
    basic_auth,
    check_public_https,
    expect_ok,
    request,
)

MAX_REPOS = 30  # branch checks cost one request per repository


def _repo_checks(provider: str, repos: list[dict]) -> list[Check]:
    """Each repo: name, public (bool), protected and secret_scanning (bool, or None if unknown)."""
    if not repos:
        return [
            Check("repositories", "Repositories", "info", f"No repositories visible in {provider}.")
        ]
    public = [r["name"] for r in repos if r["public"]]
    checked = [r for r in repos if r["protected"] is not None]
    unprotected = [r["name"] for r in checked if not r["protected"]]
    checks = [
        Check("repositories", "Repositories", "info", f"{len(repos)} repositories checked."),
        Check(
            "public_repositories",
            "Public repositories",
            "warn" if public else "pass",
            (
                "Public: "
                + ", ".join(public[:10])
                + ". Make sure none hold personal data or secrets."
            )
            if public
            else "No public repositories.",
            SECURITY,
        ),
    ]
    if checked:
        checks.append(
            Check(
                "branch_protection",
                "Default branch protection",
                "fail" if unprotected else "pass",
                ("Unprotected: " + ", ".join(unprotected[:10]))
                if unprotected
                else f"The default branch is protected in all {len(checked)} repositories checked.",
                SECURITY,
            )
        )
    else:
        checks.append(
            Check(
                "branch_protection",
                "Default branch protection",
                "info",
                "Couldn't read branch protection with these permissions.",
                SECURITY,
            )
        )
    scanning = [r for r in repos if r.get("secret_scanning") is not None]
    if scanning:
        off = [r["name"] for r in scanning if not r["secret_scanning"]]
        checks.append(
            Check(
                "secret_scanning",
                "Secret scanning",
                "warn" if off else "pass",
                ("Off in: " + ", ".join(off[:10])) if off else "On in every repository checked.",
                SECURITY,
            )
        )
    return checks


# ---- GitHub


def _gh(path: str, token: str):
    return request(
        "GET",
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json",
        },
    )


def github_test(config: dict, secrets: dict) -> str:
    user = expect_ok(_gh("/user", secrets["token"]), "GitHub sign-in")
    return f"Signed in to GitHub as {user['login']}"


def github_collect(config: dict, secrets: dict) -> list[Check]:
    token = secrets["token"]
    repos = expect_ok(_gh("/user/repos?per_page=100&sort=updated", token), "Listing repositories")
    return github_repo_checks(repos, token)


def github_repo_checks(repos_raw: list[dict], token: str) -> list[Check]:
    """Checks on GitHub repositories, given their API objects and a token that can read them."""
    repos = []
    for r in repos_raw[:MAX_REPOS]:
        branch = _gh(
            f"/repos/{r['full_name']}/branches/{urllib.parse.quote(r['default_branch'])}", token
        )
        analysis = (r.get("security_and_analysis") or {}).get("secret_scanning") or {}
        repos.append(
            {
                "name": r["full_name"],
                "public": not r["private"],
                "protected": branch.body.get("protected") if branch.ok else None,
                "secret_scanning": (analysis["status"] == "enabled")
                if "status" in analysis
                else None,
            }
        )
    return _repo_checks("GitHub", repos)


# ---- GitLab


def _gitlab_base(config: dict) -> str:
    base = (config.get("base_url") or "https://gitlab.com").rstrip("/")
    if base != "https://gitlab.com":
        check_public_https(base)
    return base


def _gl(config: dict, path: str, token: str):
    return request("GET", f"{_gitlab_base(config)}/api/v4{path}", headers={"PRIVATE-TOKEN": token})


def gitlab_test(config: dict, secrets: dict) -> str:
    user = expect_ok(_gl(config, "/user", secrets["token"]), "GitLab sign-in")
    return f"Signed in to GitLab as {user['username']}"


def gitlab_collect(config: dict, secrets: dict) -> list[Check]:
    token = secrets["token"]
    projects = expect_ok(
        _gl(config, "/projects?membership=true&per_page=100&order_by=last_activity_at", token),
        "Listing projects",
    )
    repos = []
    for p in projects[:MAX_REPOS]:
        protected_resp = _gl(config, f"/projects/{p['id']}/protected_branches", token)
        protected = None
        if protected_resp.ok and p.get("default_branch"):
            protected = any(b.get("name") == p["default_branch"] for b in protected_resp.body)
        repos.append(
            {
                "name": p["path_with_namespace"],
                "public": p.get("visibility") == "public",
                "protected": protected,
            }
        )
    return _repo_checks("GitLab", repos)


# ---- Bitbucket (API token + Atlassian account email; app passwords are retired)


def _bb(url: str, secrets: dict):
    if not url.startswith("https://api.bitbucket.org/2.0/"):
        raise ConnectorError("Unexpected Bitbucket address.")
    return request(
        "GET", url, headers={"Authorization": basic_auth(secrets["email"], secrets["token"])}
    )


def bitbucket_test(config: dict, secrets: dict) -> str:
    user = expect_ok(_bb("https://api.bitbucket.org/2.0/user", secrets), "Bitbucket sign-in")
    return f"Signed in to Bitbucket as {user.get('display_name') or user.get('nickname')}"


def bitbucket_collect(config: dict, secrets: dict) -> list[Check]:
    page = expect_ok(
        _bb("https://api.bitbucket.org/2.0/repositories?role=member&pagelen=100", secrets),
        "Listing repositories",
    )
    repos = []
    for r in page.get("values", [])[:MAX_REPOS]:
        main = (r.get("mainbranch") or {}).get("name")
        restrictions = _bb(
            f"https://api.bitbucket.org/2.0/repositories/{r['full_name']}/branch-restrictions",
            secrets,
        )
        protected = None
        if restrictions.ok and main:
            # A restriction covers the main branch by name, by a catch-all pattern, or
            # through the branching model (the main branch is its "development" branch).
            protected = any(
                x.get("pattern") in (main, "*")
                or (
                    x.get("branch_match_kind") == "branching_model"
                    and x.get("branch_type") == "development"
                )
                for x in restrictions.body.get("values", [])
            )
        repos.append(
            {
                "name": r["full_name"],
                "public": not r.get("is_private", True),
                "protected": protected,
            }
        )
    return _repo_checks("Bitbucket", repos)
