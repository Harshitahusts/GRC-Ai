import base64
import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from grc_agent.connectors import (
    BY_ID,
    CONNECTORS,
    ConnectorError,
    base,
    by_category,
    chat,
    cloud,
    vcs,
)
from grc_agent.connectors.base import Response
from grc_agent.connectors.secrets import SecretBox, mask


class FakeHTTP:
    """Routes requests by URL substring to canned responses; records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for part, resp in self.routes.items():
            if part in url:
                return resp(url, kwargs) if callable(resp) else resp
        return Response(404, {"message": "Not Found"})


def patch_http(monkeypatch, module, routes):
    fake = FakeHTTP(routes)
    monkeypatch.setattr(module, "request", fake)
    return fake


def by_key(checks):
    return {c.key: c for c in checks}


# ---- catalog


def test_catalog_is_consistent():
    ids = [c.id for c in CONNECTORS]
    assert len(ids) == len(set(ids))
    for c in CONNECTORS:
        if c.status == "available":
            assert c.fields and c.setup and c.permissions, c.id
            assert c.send if c.kind == "notify" else c.test and c.collect, c.id
    available = {c.id for c in CONNECTORS if c.status == "available"}
    assert available == {
        "github",
        "gitlab",
        "bitbucket",
        "aws",
        "gcp",
        "azure",
        "slack",
        "teams",
        "google_chat",
    }
    assert all(items for _, items in by_category())


# ---- secrets


def test_secrets_round_trip_and_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GRC_CONNECTOR_KEY", raising=False)
    box = SecretBox.for_data_dir(tmp_path)
    sealed = box.seal({"token": "ghp_secret123"})
    assert "ghp_secret123" not in sealed
    assert SecretBox.for_data_dir(tmp_path).open(sealed) == {"token": "ghp_secret123"}
    with pytest.raises(ValueError, match="can't be decrypted"):
        SecretBox(Fernet.generate_key()).open(sealed)
    assert mask("ghp_secret123") == "••••t123" and mask("short") == "••••"


# ---- URL safety


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://hooks.slack.com/services/T/B/X", True),
        ("http://hooks.slack.com/services/T/B/X", False),
        ("https://hooks.slack.com.evil.example/services", False),
        ("https://evil.example/?hooks.slack.com", False),
    ],
)
def test_slack_host_check(url, ok):
    if ok:
        base.check_host(url, chat.SLACK_HOSTS, "Slack")
    else:
        with pytest.raises(ConnectorError):
            base.check_host(url, chat.SLACK_HOSTS, "Slack")


def test_private_hosts_are_rejected():
    for url in ("https://127.0.0.1", "https://localhost", "http://gitlab.com"):
        with pytest.raises(ConnectorError):
            base.check_public_https(url)


# ---- chat


def test_chat_payloads(monkeypatch):
    fake = patch_http(monkeypatch, chat, {"": Response(200, "ok")})
    chat.slack_send({}, {"webhook_url": "https://hooks.slack.com/services/a"}, "hi")
    chat.google_chat_send({}, {"webhook_url": "https://chat.googleapis.com/v1/spaces/x"}, "hi")
    chat.teams_send({}, {"webhook_url": "https://abc.environment.api.powerplatform.com/x"}, "hi")
    slack, gchat, teams = (c[2]["json_body"] for c in fake.calls)
    assert slack == {"text": "hi"} and gchat == {"text": "hi"}
    card = teams["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["body"][0]["text"] == "hi"


def test_chat_rejects_wrong_host_and_deleted_webhook(monkeypatch):
    with pytest.raises(ConnectorError, match="Teams"):
        chat.teams_send({}, {"webhook_url": "https://outlook.office.com/webhook/x"}, "hi")
    patch_http(monkeypatch, chat, {"": Response(404, "no_service")})
    with pytest.raises(ConnectorError, match="deleted"):
        chat.slack_send({}, {"webhook_url": "https://hooks.slack.com/services/a"}, "hi")


# ---- version control


def test_github(monkeypatch):
    repos = [
        {
            "full_name": "acme/app",
            "private": True,
            "default_branch": "main",
            "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
        },
        {
            "full_name": "acme/site",
            "private": False,
            "default_branch": "main",
            "security_and_analysis": {"secret_scanning": {"status": "disabled"}},
        },
    ]
    fake = patch_http(
        monkeypatch,
        vcs,
        {
            "/user/repos": Response(200, repos),
            "/repos/acme/app/branches/main": Response(200, {"protected": True}),
            "/repos/acme/site/branches/main": Response(200, {"protected": False}),
            "/user": Response(200, {"login": "harshit"}),
        },
    )
    assert vcs.github_test({}, {"token": "t"}) == "Signed in to GitHub as harshit"
    checks = by_key(vcs.github_collect({}, {"token": "t"}))
    assert (
        checks["public_repositories"].status == "warn"
        and "acme/site" in checks["public_repositories"].detail
    )
    assert (
        checks["branch_protection"].status == "fail"
        and "acme/site" in checks["branch_protection"].detail
    )
    assert checks["secret_scanning"].status == "warn"
    assert checks["branch_protection"].provisions == ["Section 8(5)"]
    assert fake.calls[0][2]["headers"]["Authorization"] == "Bearer t"


def test_github_bad_token(monkeypatch):
    patch_http(monkeypatch, vcs, {"/user": Response(401, {"message": "Bad credentials"})})
    with pytest.raises(ConnectorError, match="access denied"):
        vcs.github_test({}, {"token": "bad"})


def test_gitlab(monkeypatch):
    patch_http(
        monkeypatch,
        vcs,
        {
            "/projects?": Response(
                200,
                [
                    {
                        "id": 1,
                        "path_with_namespace": "acme/api",
                        "visibility": "private",
                        "default_branch": "main",
                    },
                ],
            ),
            "/projects/1/protected_branches": Response(200, [{"name": "main"}]),
        },
    )
    checks = by_key(vcs.gitlab_collect({}, {"token": "t"}))
    assert checks["public_repositories"].status == "pass"
    assert checks["branch_protection"].status == "pass"


def test_gitlab_self_managed_must_be_public_https():
    with pytest.raises(ConnectorError):
        vcs.gitlab_test({"base_url": "https://127.0.0.1"}, {"token": "t"})


def test_bitbucket_uses_email_and_token(monkeypatch):
    fake = patch_http(
        monkeypatch,
        vcs,
        {
            "/repositories?role=member": Response(
                200,
                {
                    "values": [
                        {
                            "full_name": "acme/web",
                            "is_private": True,
                            "mainbranch": {"name": "main"},
                        },
                    ]
                },
            ),
            "/branch-restrictions": Response(
                200, {"values": [{"kind": "push", "pattern": "main"}]}
            ),
        },
    )
    checks = by_key(vcs.bitbucket_collect({}, {"email": "a@b.in", "token": "t"}))
    assert checks["branch_protection"].status == "pass"
    auth = fake.calls[0][2]["headers"]["Authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == "a@b.in:t"


def test_branch_protection_unknown_without_permission(monkeypatch):
    patch_http(
        monkeypatch,
        vcs,
        {
            "/user/repos": Response(
                200, [{"full_name": "a/b", "private": True, "default_branch": "main"}]
            ),
            "/branches/": Response(403, {}),
        },
    )
    assert by_key(vcs.github_collect({}, {"token": "t"}))["branch_protection"].status == "info"


# ---- AWS (a fake boto3 session)


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": ""}}, "op")


class FakeAWS:
    def __init__(self, buckets, **overrides):
        methods = {
            "sts": {"get_caller_identity": lambda: {"Account": "123456789012"}},
            "iam": {
                "get_account_summary": lambda: {"SummaryMap": {"AccountMFAEnabled": 0}},
                "get_account_password_policy": lambda: (_ for _ in ()).throw(
                    _client_error("NoSuchEntity")
                ),
            },
            "cloudtrail": {
                "describe_trails": lambda: {"trailList": [{"IsMultiRegionTrail": True}]}
            },
            "s3control": {
                "get_public_access_block": lambda AccountId: {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    }
                }
            },
            "s3": {
                "list_buckets": lambda: {"Buckets": [{"Name": n} for n in buckets]},
                "get_bucket_location": lambda Bucket: {"LocationConstraint": buckets[Bucket]},
            },
        }
        for service, fns in overrides.items():
            methods[service].update(fns)
        self.methods = methods

    def client(self, name):
        return SimpleNamespace(**self.methods[name])


def test_aws_checks_and_data_location():
    session = FakeAWS({"crm-exports": None, "app-data": "ap-south-1", "backups": "eu-west-1"})
    assert cloud.aws_test({}, {}, session=session) == "Connected to AWS account 123456789012"
    checks = by_key(cloud.aws_collect({}, {}, session=session))
    assert checks["root_mfa"].status == "fail"
    assert checks["password_policy"].status == "fail"
    assert checks["audit_logging"].status == "pass"
    assert checks["public_access"].status == "pass"
    loc = checks["data_location"]
    assert loc.status == "warn" and loc.provisions == ["Section 16(1)"]
    assert loc.data["outside_india"] == ["eu-west-1", "us-east-1"]  # None means us-east-1
    assert loc.data["inside_india"] == ["ap-south-1"]


def test_aws_access_denied_is_reported_not_crashed():
    session = FakeAWS(
        {},
        iam={"get_account_summary": lambda: (_ for _ in ()).throw(_client_error("AccessDenied"))},
    )
    check = by_key(cloud.aws_collect({}, {}, session=session))["root_mfa"]
    assert check.status == "info" and "AccessDenied" in check.detail


def test_aws_bad_credentials():
    session = FakeAWS(
        {},
        sts={
            "get_caller_identity": lambda: (_ for _ in ()).throw(
                _client_error("InvalidClientTokenId")
            )
        },
    )
    with pytest.raises(ConnectorError, match="InvalidClientTokenId"):
        cloud.aws_test({}, {}, session=session)


# ---- Google Cloud


@pytest.fixture(scope="module")
def gcp_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return private, {
        "client_email": "grc@acme-prod.iam.gserviceaccount.com",
        "private_key": pem,
        "project_id": "acme-prod",
    }


def test_gcp_jwt_is_signed_correctly(gcp_key):
    private, key = gcp_key
    token = cloud.gcp_jwt(key, now=1_700_000_000)
    head, claims, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    body = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert body["iss"] == key["client_email"] and body["aud"] == cloud.GCP_TOKEN_URI
    assert body["exp"] - body["iat"] == 3600
    private.public_key().verify(
        base64.urlsafe_b64decode(pad(sig)),
        f"{head}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_gcp_buckets(monkeypatch, gcp_key):
    _, key = gcp_key
    patch_http(
        monkeypatch,
        cloud,
        {
            "oauth2.googleapis.com/token": Response(200, {"access_token": "ya29"}),
            "storage/v1/b?project=acme-prod": Response(
                200,
                {
                    "items": [
                        {
                            "name": "in-data",
                            "location": "ASIA-SOUTH1",
                            "iamConfiguration": {
                                "publicAccessPrevention": "enforced",
                                "uniformBucketLevelAccess": {"enabled": True},
                            },
                        },
                        {
                            "name": "us-logs",
                            "location": "US",
                            "iamConfiguration": {
                                "publicAccessPrevention": "inherited",
                                "uniformBucketLevelAccess": {"enabled": False},
                            },
                        },
                    ]
                },
            ),
        },
    )
    checks = by_key(cloud.gcp_collect({}, {"service_account_json": json.dumps(key)}))
    assert checks["data_location"].data["outside_india"] == ["us"]
    assert checks["public_access"].status == "warn" and "us-logs" in checks["public_access"].detail


def test_gcp_rejects_bad_key():
    with pytest.raises(ConnectorError, match="whole service account key"):
        cloud.gcp_test({}, {"service_account_json": "{not json"})


# ---- Azure


def test_azure(monkeypatch):
    fake = patch_http(
        monkeypatch,
        cloud,
        {
            "login.microsoftonline.com/acme.onmicrosoft.com": Response(
                200, {"access_token": "eyJ"}
            ),
            "/subscriptions?api-version": Response(200, {"value": [{"subscriptionId": "s1"}]}),
            "/subscriptions/s1/resources?": Response(
                200,
                {
                    "value": [
                        {"name": "db1", "location": "centralindia"},
                        {"name": "vm2", "location": "eastus"},
                        {"name": "dns", "location": "global"},
                    ]
                },
            ),
            "storageAccounts": Response(
                200,
                {
                    "value": [
                        {
                            "name": "stor1",
                            "properties": {
                                "allowBlobPublicAccess": True,
                                "supportsHttpsTrafficOnly": True,
                                "minimumTlsVersion": "TLS1_2",
                            },
                        },
                    ]
                },
            ),
        },
    )
    secrets = {"tenant_id": "acme.onmicrosoft.com", "client_id": "c", "client_secret": "s"}
    assert "1 subscription" in cloud.azure_test({}, secrets)
    checks = by_key(cloud.azure_collect({}, secrets))
    assert checks["data_location"].data == {
        "outside_india": ["eastus"],
        "inside_india": ["centralindia"],
    }
    assert checks["public_access"].status == "warn"
    assert checks["encryption_in_transit"].status == "pass"
    assert fake.calls[0][2]["form"]["grant_type"] == "client_credentials"


def test_azure_rejects_odd_tenant():
    with pytest.raises(ConnectorError, match="tenant"):
        cloud.azure_test({}, {"tenant_id": "../evil", "client_id": "c", "client_secret": "s"})


def test_all_storage_in_india_passes():
    check = base.location_check("X", {"ap-south-1": ["a"]}, lambda r: r == "ap-south-1")
    assert check.status == "pass"


def test_registry_points_at_the_implementations():
    assert BY_ID["github"].collect is vcs.github_collect
    assert BY_ID["teams"].send is chat.teams_send
