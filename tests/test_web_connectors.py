import re
from dataclasses import replace

import pytest
from helpers import ALL_YES, create, post

from grc_agent.connectors import BY_ID, Check, ConnectorError


def fake_evidence_connector(outside=("us-east-1",), fail=False):
    def test(config, secrets):
        if "bad" in config["role_arn"]:
            raise ConnectorError("Couldn't assume the client's role (AccessDenied).")
        return "Connected to AWS account 123"

    def collect(config, secrets):
        if fail:
            raise ConnectorError("Couldn't reach sts.amazonaws.com")
        return [
            Check("root_mfa", "MFA on the root account", "fail", "No MFA.", ["Section 8(5)"]),
            Check(
                "data_location",
                "Where data is stored",
                "warn" if outside else "pass",
                "…",
                ["Section 16(1)"],
                {"outside_india": list(outside), "inside_india": ["ap-south-1"]},
            ),
        ]

    return replace(BY_ID["aws"], test=test, collect=collect)


class Outbox:
    def __init__(self):
        self.messages = []

    def send(self, config, secrets, text):
        self.messages.append((secrets["webhook_url"], text))


@pytest.fixture
def app_with_fakes(authed, monkeypatch):
    outbox = Outbox()
    authed.app.state.connectors = {
        **authed.app.state.connectors,
        "aws": fake_evidence_connector(),
        # Slack is "Coming soon"; switched on here to test notifications.
        "slack": replace(BY_ID["slack"], status="available", send=outbox.send),
    }
    monkeypatch.setattr("grc_agent.connectors.cloud.firm_account_id", lambda: "999999999999")
    return authed, outbox


AWS = {"connector": "aws", "role_arn": "arn:aws:iam::123456789012:role/grc-read-only"}
BAD_ROLE = {**AWS, "role_arn": "arn:aws:iam::123456789012:role/bad"}


def test_pages_require_login(client):
    for path in ["/connectors", "/engagements/1/connectors"]:
        assert client.get(path, follow_redirects=False).status_code == 303


def test_catalog_page_shows_working_connectors_and_a_roadmap_line(authed):
    page = authed.get("/connectors").text
    assert page.count('class="connector"') == 10
    for cid in ("github", "gitlab", "aws", "gcp", "azure", "entra_id", "slack", "teams"):
        assert f'href="/connectors/{cid}"' in page
    assert "Coming soon" not in page
    roadmap = page.split("On the roadmap:")[1]
    for name in ["Okta", "Keka", "Jira", "Google Workspace"]:
        assert name in roadmap
    assert "GitLab" not in roadmap and "Microsoft Entra ID" not in roadmap


def test_aws_role_connection_collects_evidence(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    page = client.get(f"/engagements/{eid}/connectors/new?type=aws&method=advanced").text
    assert "999999999999" in page and "grc-" in page  # firm account and external ID
    page = post(client, f"/engagements/{eid}/connectors", AWS).text
    assert "Connected to AWS account 123" in page
    assert "MFA on the root account" in page and "Where data is stored" in page
    assert "arn:aws:iam::123456789012:role/grc-read-only" in page


def test_failed_role_is_not_saved(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    page = post(client, f"/engagements/{eid}/connectors", BAD_ROLE).text
    assert "Couldn&#39;t connect to Amazon Web Services" in page
    assert "Run checks again" not in client.get(f"/engagements/{eid}/connectors").text


def test_missing_fields(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    page = post(client, f"/engagements/{eid}/connectors", {"connector": "aws"}).text
    assert "Fill in: Role ARN from the client" in page


def test_planned_connectors_cannot_be_added(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    assert client.get(f"/engagements/{eid}/connectors/new?type=okta").status_code == 404
    assert post(client, f"/engagements/{eid}/connectors", {"connector": "okta"}).status_code == 404


def test_contradiction_with_intake_blocks_delivery(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    post(
        client, f"/engagements/{eid}/intake", {**ALL_YES, "q_CTX-FOREIGN": "no", "action": "submit"}
    )
    post(client, f"/engagements/{eid}/connectors", AWS)
    findings = post(client, f"/engagements/{eid}/assess").text
    assert "answered No to using services outside India" in findings
    assert "us-east-1" in findings
    assert "Connector evidence" in findings  # linked to the obligation citing Section 8(5)
    overview = client.get(f"/engagements/{eid}").text
    assert "Intake answers match connector evidence" in overview
    assert re.search(r'<li class="todo">.*?Intake answers match connector evidence', overview, re.S)

    # Correcting the answer clears it.
    post(
        client, f"/engagements/{eid}/intake", {**ALL_YES, "q_CTX-FOREIGN": "yes", "action": "save"}
    )
    assert (
        "answered No to using services outside India" not in client.get(f"/engagements/{eid}").text
    )


def test_notifications_on_workflow_steps(app_with_fakes):
    client, outbox = app_with_fakes
    eid = create(client)
    post(
        client,
        f"/engagements/{eid}/connectors",
        {"connector": "slack", "webhook_url": "https://hooks.slack.com/services/T/B/X"},
    )
    assert "Connected for engagement Acme Pvt Ltd" in outbox.messages[-1][1]
    post(client, f"/engagements/{eid}/intake", {**ALL_YES, "action": "submit"})
    post(client, f"/engagements/{eid}/assess")
    post(client, f"/engagements/{eid}/documents/generate")
    texts = [t for _, t in outbox.messages]
    assert any("intake submitted" in t for t in texts)
    assert any("assessment run" in t and "13 findings" in t for t in texts)
    assert any("draft pack ready for review" in t for t in texts)
    assert all("Names, emails" not in t for t in texts)  # no client personal data in chat


def test_sync_error_is_recorded_and_remove_deletes(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    post(client, f"/engagements/{eid}/connectors", AWS)
    client.app.state.connectors["aws"] = fake_evidence_connector(fail=True)
    cid = re.search(r"/connectors/(\d+)/sync", client.get(f"/engagements/{eid}/connectors").text)[1]
    page = post(client, f"/engagements/{eid}/connectors/{cid}/sync").text
    assert "Couldn&#39;t reach sts.amazonaws.com" in page and "badge-fail" in page
    page = post(client, f"/engagements/{eid}/connectors/{cid}/delete").text
    assert "stored credentials deleted" in page and "Run checks again" not in page


def test_roadmap_connectors_cannot_be_added_but_built_ones_can(authed):
    eid = create(authed)
    for cid in ("okta", "jira", "keka"):
        assert authed.get(f"/engagements/{eid}/connectors/new?type={cid}").status_code == 404
    for cid in ("slack", "gitlab", "gcp", "azure", "entra_id"):
        assert authed.get(f"/engagements/{eid}/connectors/new?type={cid}").status_code == 200


def test_detail_page_and_connect_flow(authed):
    eid = create(authed)
    page = authed.get("/connectors/aws").text
    assert "SecurityAudit" in page and "For which engagement?" in page
    assert f'<option value="{eid}">Acme Pvt Ltd (SaaS)</option>' in page

    response = authed.get(f"/connectors/aws/connect?engagement={eid}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/engagements/{eid}/connectors/new?type=aws"
    # The quick way first: paste a read-only access key; the role flow is the advanced option.
    page = authed.get(response.headers["location"]).text
    assert "Access key ID" in page and "SecurityAudit" in page and "method=advanced" in page
    assert (
        "One-time setup first" in authed.get(response.headers["location"] + "&method=advanced").text
    )


def test_detail_page_without_engagements_and_for_planned(authed):
    assert "Agent-assisted</strong> client engagement" in authed.get("/connectors/github").text
    assert authed.get("/connectors/okta").status_code == 404  # planned: not shown
    assert authed.get("/connectors/okta/connect?engagement=1").status_code == 404
    page = authed.get("/connectors").text
    assert "On the roadmap:" in page and "Okta" in page and 'href="/connectors/okta"' not in page
    assert authed.get("/connectors/nope").status_code == 404


def test_connect_rejects_missing_engagements(authed):
    assert authed.get("/connectors/aws/connect?engagement=999").status_code == 404


# ---- the quick way: paste a read-only key


def test_aws_access_key_connection(authed, monkeypatch):
    from test_connectors import FakeAWS

    from grc_agent.connectors import cloud

    sessions = []

    def fake_session(config, secrets):
        sessions.append((config, secrets))
        return FakeAWS({"crm": "us-east-1"})

    monkeypatch.setattr(cloud, "aws_key_session", fake_session)
    eid = create(authed)
    page = post(
        authed,
        f"/engagements/{eid}/connectors",
        {
            "connector": "aws",
            "method": "keys",
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "x" * 40,
            "region": "ap-south-1",
        },
    ).text
    assert "Connected to AWS account 123456789012" in page and "Where data is stored" in page
    assert "pasted key" in page and "••••MPLE" in page  # only a masked hint is shown
    assert "x" * 40 not in page
    # Keys are kept apart from settings, and re-syncing uses them again.
    config, secrets = sessions[-1]
    assert config == {"region": "ap-south-1", "method": "keys"}
    assert set(secrets) == {"access_key_id", "secret_access_key"}
    cid = re.search(rf"/engagements/{eid}/connectors/(\d+)/sync", page).group(1)
    post(authed, f"/engagements/{eid}/connectors/{cid}/sync")
    assert len(sessions) >= 3


def test_aws_keys_are_checked_before_saving(authed):
    eid = create(authed)
    page = post(
        authed,
        f"/engagements/{eid}/connectors",
        {"connector": "aws", "method": "keys", "access_key_id": "nope", "secret_access_key": "s"},
    ).text
    assert "doesn&#39;t look like an AWS access key ID" in page
    assert "Run checks again" not in authed.get(f"/engagements/{eid}/connectors").text


def test_github_token_connection(authed, monkeypatch):
    from test_connectors import patch_http

    from grc_agent.connectors import vcs
    from grc_agent.connectors.base import Response

    patch_http(
        monkeypatch,
        vcs,
        {
            "/orgs/acme-labs/repos": Response(
                200,
                [{"full_name": "acme-labs/app", "private": False, "default_branch": "main"}],
            ),
            "/repos/acme-labs/app/branches/main": Response(200, {"protected": False}),
            "/users/acme-labs": Response(200, {"login": "acme-labs", "type": "Organization"}),
            "/user": Response(200, {"login": "harshit"}),
        },
    )
    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/connectors/new?type=github").text
    assert "Personal access token" in page and "Fine-grained tokens" in page
    page = post(
        authed,
        f"/engagements/{eid}/connectors",
        {
            "connector": "github",
            "method": "keys",
            "owner": "acme-labs",
            "token": "github_pat_123456",
        },
    ).text
    assert "checking organization acme-labs" in page
    assert "Public repositories" in page and "acme-labs/app" in page


def test_github_unknown_owner_is_refused(authed, monkeypatch):
    from test_connectors import patch_http

    from grc_agent.connectors import vcs
    from grc_agent.connectors.base import Response

    patch_http(
        monkeypatch, vcs, {"/users/": Response(404, {}), "/user": Response(200, {"login": "h"})}
    )
    eid = create(authed)
    page = post(
        authed,
        f"/engagements/{eid}/connectors",
        {"connector": "github", "method": "keys", "owner": "ghost-org", "token": "github_pat_1"},
    ).text
    assert "no GitHub user or organisation called ghost-org" in page
