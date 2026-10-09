"""Identity connectors: Microsoft Entra ID (Microsoft 365).

Read-only checks on who can sign in and how, as evidence for reasonable security
safeguards (Section 8(5); Rule 6 names access control): multi-factor authentication,
sign-in policies, how many people hold the most powerful admin role, and guest accounts.

Uses an app registration with read-only Microsoft Graph application permissions. A check
the app has no permission (or licence) for comes back as "info", never as a pass.
"""

from __future__ import annotations

from grc_agent.connectors.base import SECURITY, Check, ConnectorError, request
from grc_agent.connectors.cloud import microsoft_token

GRAPH = "https://graph.microsoft.com"
GLOBAL_ADMIN = "62e90394-69f5-4237-9190-012177145e10"  # role template id, the same in every tenant
MAX_GLOBAL_ADMINS = 4  # Microsoft recommends fewer than five Global Administrators


class _NoAccess(Exception):
    """Graph said no (missing permission or licence); `reason` says which."""

    def __init__(self, reason: str):
        self.reason = reason


def _token(secrets: dict) -> str:
    return microsoft_token(secrets, GRAPH, "Microsoft Entra ID")


def _get(url: str, token: str) -> dict:
    if not url.startswith(GRAPH + "/"):
        raise ConnectorError("Unexpected Microsoft Graph address.")
    resp = request("GET", url, headers={"Authorization": f"Bearer {token}"})
    if resp.status in (401, 403):
        err = resp.body.get("error", {}) if isinstance(resp.body, dict) else {}
        code, msg = str(err.get("code", "")), str(err.get("message", ""))
        if "NonPremium" in code or "premium" in msg.lower() or "license" in msg.lower():
            raise _NoAccess("needs a Microsoft Entra ID P1 or P2 licence")
        raise _NoAccess("the app doesn't have the permission for this")
    if not resp.ok:
        raise ConnectorError(f"Microsoft Graph returned HTTP {resp.status}.")
    return resp.body if isinstance(resp.body, dict) else {}


def _list(url: str, token: str, pages: int = 10) -> list[dict]:
    items: list[dict] = []
    while url and pages:
        body = _get(url, token)
        items += body.get("value", [])
        url, pages = body.get("@odata.nextLink"), pages - 1
    return items


def entra_test(config: dict, secrets: dict) -> str:
    token = _token(secrets)
    try:
        orgs = _list(f"{GRAPH}/v1.0/organization?$select=displayName", token, pages=1)
    except _NoAccess:
        raise ConnectorError(
            "Signed in, but the app can't read the directory. Add the Microsoft Graph "
            "application permissions listed below and grant admin consent."
        ) from None
    name = orgs[0].get("displayName") if orgs else "your organisation"
    return f"Connected to Microsoft Entra ID for {name}"


def _info(key: str, title: str, reason: str) -> Check:
    return Check(key, title, "info", f"Couldn't check: {reason}.", SECURITY)


def _mfa_check(token: str) -> Check:
    title = "Multi-factor authentication"
    try:
        rows = _list(
            f"{GRAPH}/v1.0/reports/authenticationMethods/userRegistrationDetails?$top=999", token
        )
    except _NoAccess as exc:
        return _info("mfa", title, exc.reason)
    members = [r for r in rows if r.get("userType", "member") == "member"]
    if not members:
        return Check("mfa", title, "info", "No member accounts found.", SECURITY)
    without = [r for r in members if not r.get("isMfaRegistered")]
    admins = [r for r in without if r.get("isAdmin")]
    share = 100 * (len(members) - len(without)) // len(members)
    names = ", ".join(r.get("userPrincipalName", "?") for r in (admins or without)[:10])
    if admins:
        status, detail = "fail", f"{len(admins)} admin account(s) have no MFA registered: {names}."
    elif without:
        status = "warn"
        detail = f"{share}% of {len(members)} people have MFA. Not registered: {names}."
    else:
        status, detail = "pass", f"All {len(members)} people have MFA registered."
    return Check("mfa", title, status, detail, SECURITY)


def _sign_in_policy_check(token: str) -> Check:
    title = "Sign-in policies (Conditional Access or security defaults)"
    try:
        defaults = _get(f"{GRAPH}/v1.0/policies/identitySecurityDefaultsEnforcementPolicy", token)
    except _NoAccess:
        defaults = {}
    if defaults.get("isEnabled"):
        return Check(
            "sign_in_policy",
            title,
            "pass",
            "Security defaults are on: MFA is required and legacy sign-in is blocked.",
            SECURITY,
        )
    try:
        policies = _list(f"{GRAPH}/v1.0/identity/conditionalAccess/policies", token)
    except _NoAccess as exc:
        return _info("sign_in_policy", title, exc.reason)
    on = [p for p in policies if p.get("state") == "enabled"]
    if on:
        names = ", ".join(p.get("displayName", "?") for p in on[:5])
        return Check(
            "sign_in_policy",
            title,
            "pass",
            f"{len(on)} Conditional Access policies on: {names}.",
            SECURITY,
        )
    return Check(
        "sign_in_policy",
        title,
        "fail",
        "Security defaults are off and no Conditional Access policy is on, so nothing "
        "requires MFA at sign-in.",
        SECURITY,
    )


def _admin_check(token: str) -> Check:
    title = "Global Administrators"
    try:
        members = _list(
            f"{GRAPH}/v1.0/directoryRoles(roleTemplateId='{GLOBAL_ADMIN}')/members"
            "?$select=userPrincipalName,displayName",
            token,
        )
    except _NoAccess as exc:
        return _info("global_admins", title, exc.reason)
    names = ", ".join(m.get("userPrincipalName") or m.get("displayName", "?") for m in members[:10])
    if len(members) > MAX_GLOBAL_ADMINS:
        return Check(
            "global_admins",
            title,
            "warn",
            f"{len(members)} Global Administrators ({names}). Microsoft recommends fewer "
            "than five; use narrower admin roles.",
            SECURITY,
        )
    return Check(
        "global_admins",
        title,
        "pass",
        f"{len(members)} Global Administrator(s): {names}.",
        SECURITY,
    )


def _accounts_check(token: str) -> Check:
    title = "Accounts"
    try:
        users = _list(f"{GRAPH}/v1.0/users?$select=userType,accountEnabled&$top=999", token)
    except _NoAccess as exc:
        return _info("accounts", title, exc.reason)
    enabled = [u for u in users if u.get("accountEnabled")]
    guests = [u for u in enabled if u.get("userType") == "Guest"]
    return Check(
        "accounts",
        title,
        "info",
        f"{len(enabled)} enabled accounts, of which {len(guests)} are guests. Review guests "
        "and leavers regularly: each one can reach the organisation's data.",
        SECURITY,
    )


def entra_collect(config: dict, secrets: dict) -> list[Check]:
    token = _token(secrets)
    return [
        _mfa_check(token),
        _sign_in_policy_check(token),
        _admin_check(token),
        _accounts_check(token),
    ]
