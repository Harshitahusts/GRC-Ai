"""GitHub App and AWS role flows: the client authorises read-only access."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from helpers import create, post

from grc_agent.connectors import ConnectorError, cloud, github_app
from grc_agent.connectors.base import Response
from grc_agent.web import db
from grc_agent.web.connector_views import external_id

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
CONFIG = github_app.AppConfig("12345", "grc-agent-test", PEM)
INSTALL = {"id": 777, "account": {"login": "AcmeOrg"}, "repository_selection": "selected"}
REPO = {"full_name": "acmeorg/api", "default_branch": "main", "private": False}


class FakeGitHub:
    """Answers the GitHub API calls the app makes; records them."""

    def __init__(self, installs=(INSTALL,), removed=False):
        self.calls, self.installs, self.removed = [], list(installs), removed

    def __call__(self, method, url, headers=None, **kwargs):
        path = url.removeprefix("https://api.github.com")
        self.calls.append((method, path, (headers or {}).get("Authorization", "")))
        if path.startswith("/app-manifests/"):
            return Response(201, {"id": 12345, "slug": "grc-agent-test", "pem": PEM})
        if path.startswith("/app/installations?"):
            return Response(200, self.installs)
        if path.endswith("/access_tokens"):
            return Response(404, {}) if self.removed else Response(201, {"token": "ghs_x"})
        if path.startswith("/app/installations/"):
            found = [i for i in self.installs if path.endswith(f"/{i['id']}")]
            return Response(200, found[0]) if found else Response(404, {})
        if path.startswith("/installation/repositories"):
            return Response(200, {"repositories": [REPO]})
        if "/branches/" in path:
            return Response(200, {"protected": False})
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr("grc_agent.connectors.github_app.request", fake)
    monkeypatch.setattr("grc_agent.connectors.vcs.request", fake)
    return fake


def _claims(token):
    header, payload, sig = token.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    KEY.public_key().verify(
        pad(sig), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return json.loads(pad(header)), json.loads(pad(payload))


# ---- GitHub App module


def test_manifest_is_read_only_without_webhooks():
    m = github_app.manifest("GRC", "http://localhost:8000", "https://firm.example")
    assert m["default_permissions"] == {"metadata": "read", "administration": "read"}
    assert set(m["default_permissions"].values()) == {"read"}
    assert m["hook_attributes"]["active"] is False and m["default_events"] == []
    assert m["redirect_url"] == "http://localhost:8000/settings/github-app/callback"
    assert m["setup_url"] == "http://localhost:8000/connectors/github/setup"


def test_app_jwt_is_signed_and_short_lived():
    header, claims = _claims(github_app.app_jwt(CONFIG, now=1_000_000))
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}
    assert claims["exp"] - claims["iat"] <= 600


def test_bad_private_key_is_a_clean_error():
    with pytest.raises(ConnectorError, match="PEM"):
        github_app.app_jwt(github_app.AppConfig("1", "x", "not a key"))


def test_convert_manifest(gh):
    with pytest.raises(ConnectorError, match="invalid code"):
        github_app.convert_manifest("../etc")
    config = github_app.convert_manifest("abc123def456")
    assert (config.app_id, config.slug, config.pem) == ("12345", "grc-agent-test", PEM)
    assert gh.calls[-1][:2] == ("POST", "/app-manifests/abc123def456/conversions")


def test_installation_lookup_and_collect(gh):
    assert github_app.find_installation(CONFIG, "acmeorg")["id"] == 777  # any case
    with pytest.raises(ConnectorError, match="isn't installed on 'other'"):
        github_app.find_installation(CONFIG, "other")
    with pytest.raises(ConnectorError, match="Invalid installation"):
        github_app.get_installation(CONFIG, "7/../1")
    checks = {c.key: c for c in github_app.collect(CONFIG, "777")}
    assert checks["public_repositories"].status == "warn"
    # the app authenticates as itself, then with the short-lived installation token
    assert gh.calls[0][2].startswith("Bearer ey")
    assert ("GET", "/installation/repositories?per_page=100", "Bearer ghs_x") in gh.calls


def test_removed_app_is_explained(gh):
    gh.removed = True
    with pytest.raises(ConnectorError, match="removed the app"):
        github_app.collect(CONFIG, "777")


def test_config_saved_encrypted(app):
    github_app.save_config(app.state.data_dir, app.state.secret_box, CONFIG)
    raw = (app.state.data_dir / github_app.CONFIG_FILE).read_text()
    assert "PRIVATE KEY" not in raw
    assert github_app.load_config(app.state.data_dir, app.state.secret_box) == CONFIG


# ---- GitHub in the web app


@pytest.fixture
def with_app(authed, gh):
    app = authed.app
    github_app.save_config(app.state.data_dir, app.state.secret_box, CONFIG)
    return authed


def test_setup_page_without_app(authed):
    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/connectors/new?type=github").text
    assert "One-time setup first" in page
    page = authed.get("/settings/github-app").text
    assert "https://github.com/settings/apps/new" in page
    assert "&#34;administration&#34;: &#34;read&#34;" in page or '"administration"' in page


def test_local_app_uses_a_public_homepage(authed, monkeypatch):
    monkeypatch.delenv("GRC_PUBLIC_URL", raising=False)
    page = authed.get("/settings/github-app").text
    assert "https://github.com/Harshitahusts/GRC-Ai/github/events" in page
    monkeypatch.setenv("GRC_PUBLIC_URL", "https://grc.firm.example/")
    assert "https://grc.firm.example/github/events" in authed.get("/settings/github-app").text


def test_manifest_callback_checks_state(authed, gh):
    response = authed.get("/settings/github-app/callback?code=abc123def456&state=forged")
    assert response.status_code == 400
    assert not gh.calls


def test_manifest_callback_saves_app(authed, gh):
    page = authed.get("/settings/github-app").text
    state = page.split("apps/new?state=")[1].split('"')[0]
    response = authed.get(
        f"/settings/github-app/callback?code=abc123def456&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    app = authed.app
    assert github_app.load_config(app.state.data_dir, app.state.secret_box).slug == "grc-agent-test"


def _install_state(client, eid):
    page = client.get(f"/engagements/{eid}/connectors/new?type=github").text
    assert "Authorize on GitHub" in page
    return page.split("installations/new?state=")[1].split('"')[0]


def test_install_callback_connects_and_collects(with_app):
    eid = create(with_app)
    state = _install_state(with_app, eid)
    url = f"/connectors/github/setup?installation_id=777&setup_action=install&state={state}"
    page = with_app.get(url).text
    assert "Authorised by GitHub account AcmeOrg" in page
    assert "Public repositories" in page
    # GitHub sends the client back after they change repositories: update, don't duplicate
    with_app.get(url.replace("install&", "update&"))
    conn = db.connect(with_app.app.state.db_path)
    assert conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 1
    config = json.loads(conn.execute("SELECT config_json FROM connections").fetchone()[0])
    assert config == {
        "installation_id": "777",
        "account": "AcmeOrg",
        "repository_selection": "selected",
    }


def test_install_callback_for_the_client(with_app, client):
    """The client installs from their own browser: they just get a thank-you page."""
    fresh = type(client)(with_app.app)
    page = fresh.get("/connectors/github/setup?installation_id=777&setup_action=install").text
    assert "Read-only access is set up" in page
    page = fresh.get("/connectors/github/setup?setup_action=request").text
    assert "Request sent" in page


def test_install_callback_rejects_other_state(with_app):
    eid = create(with_app)
    page = with_app.get(f"/connectors/github/setup?installation_id=777&state=forged-{eid}").text
    assert "Read-only access is set up" in page  # treated as not ours; nothing saved
    assert "AcmeOrg" not in with_app.get(f"/engagements/{eid}/connectors").text


def test_find_installation_by_account(with_app):
    eid = create(with_app)
    find = f"/engagements/{eid}/connectors/github/find"
    page = post(with_app, find, {"account": "nobody"}).text
    assert "isn&#39;t installed on &#39;nobody&#39;" in page
    page = post(with_app, find, {"account": "acmeorg"}).text
    assert "Found the app installed on AcmeOrg" in page


def test_github_cannot_be_added_with_a_token(with_app):
    eid = create(with_app)
    data = {"connector": "github", "token": "ghp_x"}
    response = post(with_app, f"/engagements/{eid}/connectors", data)
    assert response.status_code == 400


# ---- AWS role


def test_external_id_is_stable_and_per_engagement(app):
    assert external_id(app, 1) == external_id(app, 1)
    assert external_id(app, 1) != external_id(app, 2)
    assert external_id(app, 1).startswith("grc-") and len(external_id(app, 1)) == 36


def test_cloudformation_template():
    body = cloud.cloudformation_template("999999999999", "grc-abc")
    assert "arn:aws:iam::999999999999:root" in body
    assert 'sts:ExternalId: "grc-abc"' in body
    assert "arn:aws:iam::aws:policy/SecurityAudit" in body
    assert "RoleArn" in body


def test_template_download(authed, monkeypatch):
    eid = create(authed)
    assert authed.get(f"/engagements/{eid}/connectors/aws/template.yaml").status_code == 400
    monkeypatch.setattr(cloud, "firm_account_id", lambda: "999999999999")
    response = authed.get(f"/engagements/{eid}/connectors/aws/template.yaml")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert external_id(authed.app, eid) in response.text


class FakeSTS:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def client(self, name):
        assert name == "sts"
        return self

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": self.error, "Message": ""}}, "AssumeRole")
        return {"Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}}


ROLE = "arn:aws:iam::123456789012:role/grc-read-only"


def test_assume_role_uses_external_id(monkeypatch):
    sts = FakeSTS()
    monkeypatch.setattr(cloud, "firm_session", lambda: sts)
    monkeypatch.setattr(cloud, "_session_from", lambda creds, region: (creds, region))
    creds, region = cloud._aws_session({"role_arn": ROLE, "external_id": "grc-x"}, {})
    assert creds["SessionToken"] == "T" and region == "ap-south-1"
    assert sts.calls == [
        {
            "RoleArn": ROLE,
            "RoleSessionName": "grc-agent",
            "ExternalId": "grc-x",
            "DurationSeconds": 900,
        }
    ]


def test_assume_role_errors(monkeypatch):
    monkeypatch.setattr(cloud, "firm_session", lambda: FakeSTS("AccessDenied"))
    with pytest.raises(ConnectorError, match="trusts this firm"):
        cloud._aws_session({"role_arn": ROLE, "external_id": "grc-x"}, {})
    for bad in ("", "arn:aws:iam::123:role/x", "arn:aws:iam::123456789012:user/x"):
        with pytest.raises(ConnectorError, match="role ARN"):
            cloud._aws_session({"role_arn": bad, "external_id": "grc-x"}, {})


def test_aws_page_without_firm_credentials(authed):
    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/connectors/new?type=aws").text
    assert "One-time setup first" in page and "sts:AssumeRole" in page
