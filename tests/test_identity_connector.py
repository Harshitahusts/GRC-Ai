"""Microsoft Entra ID connector: MFA, sign-in policies, Global Administrators, accounts."""

import pytest
from test_connectors import patch_http

from grc_agent.connectors import BY_ID, ConnectorError, cloud, identity
from grc_agent.connectors.base import Response

SECRETS = {"tenant_id": "contoso.onmicrosoft.com", "client_id": "app-id", "client_secret": "s3cret"}
TOKEN = {"login.microsoftonline.com": Response(200, {"access_token": "graph-token"})}
FORBIDDEN = Response(
    403, {"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}}
)
NO_LICENCE = Response(
    403,
    {
        "error": {
            "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
            "message": "Neither tenant is B2C or tenant doesn't have premium license",
        }
    },
)


def graph(monkeypatch, routes):
    patch_http(monkeypatch, cloud, TOKEN)
    return patch_http(monkeypatch, identity, routes)


def registrations(*rows):
    return Response(200, {"value": [dict(userType="member", **r) for r in rows]})


def checks_by_key(monkeypatch, routes):
    graph(monkeypatch, routes)
    return {c.key: c for c in identity.entra_collect({}, SECRETS)}


def test_entra_is_available_with_read_only_permissions():
    c = BY_ID["entra_id"]
    assert c.status == "available" and c.category == "Identity and access"
    assert "Read" in c.permissions and "Write" not in c.permissions


def test_test_names_the_organisation_and_asks_for_the_graph_token(monkeypatch):
    patch = graph(
        monkeypatch, {"/organization": Response(200, {"value": [{"displayName": "Acme Pvt Ltd"}]})}
    )
    assert identity.entra_test({}, SECRETS) == "Connected to Microsoft Entra ID for Acme Pvt Ltd"
    assert patch.calls[0][2]["headers"]["Authorization"] == "Bearer graph-token"


def test_token_is_for_microsoft_graph(monkeypatch):
    token = patch_http(monkeypatch, cloud, TOKEN)
    patch_http(monkeypatch, identity, {"/organization": Response(200, {"value": []})})
    identity.entra_test({}, SECRETS)
    form = token.calls[0][2]["form"]
    assert form["scope"] == "https://graph.microsoft.com/.default"
    assert form["grant_type"] == "client_credentials"


def test_rejected_secret_and_missing_permissions(monkeypatch):
    patch_http(
        monkeypatch,
        cloud,
        {"login.microsoftonline.com": Response(401, {"error": "invalid_client"})},
    )
    with pytest.raises(ConnectorError, match="rejected the tenant ID"):
        identity.entra_test({}, SECRETS)
    graph(monkeypatch, {"/organization": FORBIDDEN})
    with pytest.raises(ConnectorError, match="grant admin consent"):
        identity.entra_test({}, SECRETS)


def test_all_good(monkeypatch):
    checks = checks_by_key(
        monkeypatch,
        {
            "userRegistrationDetails": registrations(
                {"userPrincipalName": "a@acme.in", "isMfaRegistered": True, "isAdmin": True},
                {"userPrincipalName": "b@acme.in", "isMfaRegistered": True, "isAdmin": False},
            ),
            "identitySecurityDefaultsEnforcementPolicy": Response(200, {"isEnabled": True}),
            "directoryRoles": Response(200, {"value": [{"userPrincipalName": "a@acme.in"}]}),
            "/users?": Response(200, {"value": [{"userType": "Member", "accountEnabled": True}]}),
        },
    )
    assert checks["mfa"].status == "pass" and "All 2 people" in checks["mfa"].detail
    assert (
        checks["sign_in_policy"].status == "pass"
        and "Security defaults are on" in checks["sign_in_policy"].detail
    )
    assert checks["global_admins"].status == "pass"
    assert checks["accounts"].status == "info" and checks["mfa"].provisions == ["Section 8(5)"]


def test_admin_without_mfa_fails_and_no_policy_fails(monkeypatch):
    checks = checks_by_key(
        monkeypatch,
        {
            "userRegistrationDetails": registrations(
                {"userPrincipalName": "boss@acme.in", "isMfaRegistered": False, "isAdmin": True},
                {"userPrincipalName": "b@acme.in", "isMfaRegistered": True, "isAdmin": False},
            ),
            "identitySecurityDefaultsEnforcementPolicy": Response(200, {"isEnabled": False}),
            "conditionalAccess/policies": Response(
                200, {"value": [{"displayName": "Old", "state": "disabled"}]}
            ),
            "directoryRoles": Response(
                200, {"value": [{"userPrincipalName": f"admin{i}@acme.in"} for i in range(6)]}
            ),
            "/users?": Response(
                200,
                {
                    "value": [
                        {"userType": "Guest", "accountEnabled": True},
                        {"userType": "Member", "accountEnabled": False},
                    ]
                },
            ),
        },
    )
    assert checks["mfa"].status == "fail" and "boss@acme.in" in checks["mfa"].detail
    assert checks["sign_in_policy"].status == "fail"
    assert (
        checks["global_admins"].status == "warn"
        and "6 Global Administrators" in checks["global_admins"].detail
    )
    assert "1 enabled accounts, of which 1 are guests" in checks["accounts"].detail


def test_members_without_mfa_warn_and_conditional_access_passes(monkeypatch):
    checks = checks_by_key(
        monkeypatch,
        {
            "userRegistrationDetails": registrations(
                {"userPrincipalName": "a@acme.in", "isMfaRegistered": True},
                {"userPrincipalName": "b@acme.in", "isMfaRegistered": False},
            ),
            "identitySecurityDefaultsEnforcementPolicy": Response(200, {"isEnabled": False}),
            "conditionalAccess/policies": Response(
                200, {"value": [{"displayName": "Require MFA", "state": "enabled"}]}
            ),
            "directoryRoles": Response(200, {"value": []}),
            "/users?": Response(200, {"value": []}),
        },
    )
    assert (
        checks["mfa"].status == "warn"
        and "50% of 2" in checks["mfa"].detail
        and "b@acme.in" in checks["mfa"].detail
    )
    assert (
        checks["sign_in_policy"].status == "pass"
        and "Require MFA" in checks["sign_in_policy"].detail
    )


def test_missing_licence_or_permission_is_info_never_pass(monkeypatch):
    checks = checks_by_key(
        monkeypatch,
        {
            "userRegistrationDetails": NO_LICENCE,
            "identitySecurityDefaultsEnforcementPolicy": FORBIDDEN,
            "conditionalAccess/policies": FORBIDDEN,
            "directoryRoles": FORBIDDEN,
            "/users?": FORBIDDEN,
        },
    )
    assert {c.status for c in checks.values()} == {"info"}
    assert "P1 or P2" in checks["mfa"].detail
    assert "permission" in checks["global_admins"].detail


def test_pages_are_followed_but_only_on_graph(monkeypatch):
    page2 = "https://graph.microsoft.com/v1.0/users?$skiptoken=2"
    patch = graph(
        monkeypatch,
        {
            "skiptoken=2": Response(
                200, {"value": [{"userType": "Member", "accountEnabled": True}]}
            ),
            "/users?": Response(
                200,
                {
                    "value": [{"userType": "Member", "accountEnabled": True}],
                    "@odata.nextLink": page2,
                },
            ),
        },
    )
    assert len(identity._list("https://graph.microsoft.com/v1.0/users?$top=999", "t")) == 2
    assert any(page2 == c[1] for c in patch.calls)
    graph(
        monkeypatch,
        {"/users?": Response(200, {"value": [], "@odata.nextLink": "https://evil.example/steal"})},
    )
    with pytest.raises(ConnectorError, match="Unexpected Microsoft Graph address"):
        identity._list("https://graph.microsoft.com/v1.0/users?$top=999", "t")
