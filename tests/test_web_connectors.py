import re
from dataclasses import replace

import pytest
from helpers import ALL_YES, create, post

from grc_agent.connectors import BY_ID, Check, ConnectorError


def fake_evidence_connector(outside=("us-east-1",), fail=False):
    def test(config, secrets):
        if secrets.get("access_key_id") == "bad":
            raise ConnectorError("AWS rejected the credentials (InvalidClientTokenId).")
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
def app_with_fakes(authed):
    outbox = Outbox()
    authed.app.state.connectors = {
        **authed.app.state.connectors,
        "aws": fake_evidence_connector(),
        "slack": replace(BY_ID["slack"], send=outbox.send),
    }
    return authed, outbox


AWS = {
    "connector": "aws",
    "access_key_id": "AKIAEXAMPLE1234",
    "secret_access_key": "s3cr3t-value-9876",
}


def test_pages_require_login(client):
    for path in ["/connectors", "/engagements/1/connectors"]:
        assert client.get(path, follow_redirects=False).status_code == 303


def test_catalog_page(authed):
    page = authed.get("/connectors").text
    for name in [
        "GitHub",
        "GitLab",
        "Bitbucket",
        "Amazon Web Services",
        "Google Cloud",
        "Microsoft Azure",
        "Slack",
        "Microsoft Teams",
        "Google Chat",
        "Okta",
        "Keka",
    ]:
        assert name in page
    assert page.count("Planned") >= 10


def test_add_evidence_connector_stores_encrypted_and_collects(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    page = post(client, f"/engagements/{eid}/connectors", AWS).text
    assert "Connected to AWS account 123" in page
    assert "MFA on the root account" in page and "Where data is stored" in page
    assert "••••9876" in page and "s3cr3t-value-9876" not in page

    import sqlite3

    db = sqlite3.connect(client.app.state.db_path)
    stored = db.execute("SELECT secrets_enc FROM connections").fetchone()[0]
    assert "s3cr3t" not in stored and "AKIA" not in stored


def test_bad_credentials_are_not_saved(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    page = post(client, f"/engagements/{eid}/connectors", {**AWS, "access_key_id": "bad"}).text
    assert "Couldn&#39;t connect to Amazon Web Services" in page
    assert "Run checks again" not in client.get(f"/engagements/{eid}/connectors").text


def test_missing_fields(app_with_fakes):
    client, _ = app_with_fakes
    eid = create(client)
    page = post(client, f"/engagements/{eid}/connectors", {"connector": "aws"}).text
    assert "Fill in: Access key ID, Secret access key" in page


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


def test_real_slack_connector_rejects_non_slack_url(authed):
    eid = create(authed)
    page = post(
        authed,
        f"/engagements/{eid}/connectors",
        {"connector": "slack", "webhook_url": "https://evil.example/hook"},
    ).text
    assert "doesn&#39;t look like a Slack incoming webhook URL" in page
