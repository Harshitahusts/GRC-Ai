import io
import re

import anthropic
import pytest
from docx import Document
from fastapi.testclient import TestClient

from grc_agent.web import cli as web_cli
from grc_agent.web.app import create_app
from grc_agent.web.security import hash_password, verify_password

PASSWORD = "correct-horse-battery"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("GRC_SECRET_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert (
        web_cli.main(["--data-dir", str(tmp_path), "adduser", "harshit", "--password-stdin"]) == 0
    )
    return create_app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


def csrf(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(client, password=PASSWORD):
    return client.post(
        "/login",
        data={"username": "harshit", "password": password, "csrf": csrf(client)},
        follow_redirects=False,
    )


@pytest.fixture
def authed(client):
    assert login(client).status_code == 303
    return client


def post(client, path, data=None, **kwargs):
    return client.post(path, data={**(data or {}), "csrf": csrf(client, "/")}, **kwargs)


# ---- auth


def test_password_hashing():
    stored = hash_password("secret-password")
    assert verify_password("secret-password", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")


def test_pages_require_login(client):
    for path in ["/", "/engagements", "/kpis", "/assistant", "/engagements/1"]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.get("/healthz").json() == {"status": "ok"}


def test_login_and_logout(client):
    assert login(client, "wrong-password").status_code == 401
    assert login(client).headers["location"] == "/"
    assert "Dashboard" in client.get("/").text
    post(client, "/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_is_locked_after_repeated_failures(client):
    for _ in range(5):
        assert login(client, "wrong-password").status_code == 401
    assert login(client).status_code == 429  # even the right password, while locked


def test_forms_reject_missing_csrf(authed):
    response = authed.post("/engagements", data={"client": "X", "sector": "SaaS"})
    assert response.status_code == 403


def test_adduser_rejects_duplicates_and_short_passwords(tmp_path, monkeypatch):
    args = ["--data-dir", str(tmp_path)]
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert web_cli.main([*args, "adduser", "a", "--password-stdin"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert web_cli.main([*args, "adduser", "a", "--password-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert web_cli.main([*args, "adduser", "A", "--password-stdin"]) == 1


# ---- workflow


def create(client, mode="agent"):
    response = post(
        client, "/engagements", {"client": "Acme Pvt Ltd", "sector": "SaaS", "mode": mode}
    )
    return int(response.url.path.rsplit("/", 1)[1])


ALL_YES = {
    "q_INFO-DATA": "Names, emails",
    "q_CTX-CHILDREN": "no",
    "q_CTX-VENDORS": "yes",
    "q_CTX-FOREIGN": "no",
    **{
        f"q_{q}": "yes"
        for q in [
            "Q-NOTICE",
            "Q-CONSENT",
            "Q-WITHDRAW",
            "Q-SECURITY",
            "Q-BREACH",
            "Q-ERASURE",
            "Q-CONTACT",
            "Q-GRIEVANCE",
            "Q-ACCESS",
            "Q-CORRECTION",
            "Q-VENDOR-CONTRACT",
        ]
    },
}


def document_ids(client, eid):
    return sorted(
        set(
            map(
                int,
                re.findall(
                    rf"/engagements/{eid}/documents/(\d+)",
                    client.get(f"/engagements/{eid}/documents").text,
                ),
            )
        )
    )


def test_full_engagement_to_north_star(authed):
    eid = create(authed)
    base = f"/engagements/{eid}"

    # Delivery is blocked from the start.
    assert "Can&#39;t deliver yet" in post(authed, f"{base}/deliver").text

    post(authed, f"{base}/intake", {**ALL_YES, "action": "submit"})
    page = post(authed, f"{base}/assess").text
    assert "Assessment complete: 13 findings" in page
    assert "✗ unresolved" not in page

    post(authed, f"{base}/documents/generate")
    docs = document_ids(authed, eid)
    assert len(docs) == 5

    # The review gate: no export before review.
    assert authed.get(f"{base}/documents/{docs[0]}/download").status_code == 403
    # Review needs the confirmation tick.
    post(authed, f"{base}/documents/{docs[0]}/review", {"outcome": "usable"})
    assert authed.get(f"{base}/documents/{docs[0]}/download").status_code == 403

    for did in docs:
        post(authed, f"{base}/documents/{did}/review", {"outcome": "minor_edits", "confirm": "on"})
    download = authed.get(f"{base}/documents/{docs[0]}/download")
    assert download.status_code == 200
    assert "Acme Pvt Ltd" in Document(io.BytesIO(download.content)).paragraphs[0].text

    post(authed, f"{base}/details", {"consultant_hours": "5", "intake_completed_unaided": "yes"})
    post(authed, f"{base}/deliver")
    assert "Delivered" in authed.get(base).text

    record = authed.get(f"{base}/export.json").json()
    assert record["completed"] and len(record["findings"]) == 13 and len(record["documents"]) == 5

    kpis = authed.get("/kpis").text
    assert "Verified Engagements Delivered: 1" in kpis

    # Delivered engagements are locked.
    assert post(authed, f"{base}/intake", {**ALL_YES, "action": "save"}).status_code == 400


def test_intake_change_marks_assessment_stale_and_blocks_delivery(authed):
    eid = create(authed)
    base = f"/engagements/{eid}"
    post(authed, f"{base}/intake", {**ALL_YES, "action": "submit"})
    post(authed, f"{base}/assess")
    post(authed, f"{base}/documents/generate")

    post(authed, f"{base}/intake", {**ALL_YES, "q_Q-NOTICE": "no", "action": "save"})
    page = authed.get(base).text
    assert "Re-run the assessment" in page
    assert post(authed, f"{base}/documents/generate").url.path.endswith("/findings")

    # Re-running clears documents so they must be regenerated and reviewed again.
    page = post(authed, f"{base}/assess").text
    assert "Documents were cleared" in page
    assert document_ids(authed, eid) == []


def test_skipped_answers_become_open_items(authed):
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", {"action": "submit"})
    page = post(authed, f"/engagements/{eid}/assess").text
    assert page.count("status-badge-open_item") == 13


def test_assessment_requires_submitted_intake(authed):
    eid = create(authed)
    page = post(authed, f"/engagements/{eid}/assess").text
    assert "Submit the intake before running the assessment" in page


def test_scoring_a_hallucination_forces_wrong(authed):
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", {"action": "submit"})
    page = post(authed, f"/engagements/{eid}/assess").text
    fid = re.search(r'id="f(\d+)"', page).group(1)
    page = post(
        authed, f"/engagements/{eid}/findings/{fid}", {"verdict": "correct", "hallucination": "on"}
    ).text
    assert "always scored wrong" in page
    assert authed.get(f"/engagements/{eid}/export.json").json()["findings"][0]["verdict"] == "wrong"


def test_manual_baseline_feeds_kpis(authed):
    eid = create(authed, mode="manual")
    post(authed, f"/engagements/{eid}/deliver")
    assert "Consultant hours recorded" in authed.get(f"/engagements/{eid}").text
    post(authed, f"/engagements/{eid}/details", {"consultant_hours": "18"})
    post(authed, f"/engagements/{eid}/deliver")
    assert "Manual baseline: 18.0 h" in authed.get("/kpis").text
    assert post(authed, f"/engagements/{eid}/intake", {"action": "save"}).status_code == 400


def test_unknown_engagement_is_404(authed):
    assert authed.get("/engagements/999").status_code == 404


class FakeAgent:
    def __init__(self, exc=None):
        self.exc, self.messages = exc, []

    def ask(self, question):
        if self.exc:
            raise self.exc
        from types import SimpleNamespace

        self.messages += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": [SimpleNamespace(type="text", text="Hi there")]},
        ]
        return SimpleNamespace(tool_calls=["score_risk"])


@pytest.mark.parametrize(
    "exc",
    [
        TypeError("Could not resolve authentication method. Expected one of api_key"),
        anthropic.CredentialsError("Config file not found"),
    ],
)
def test_assistant_without_credentials(authed, monkeypatch, exc):
    monkeypatch.setattr("grc_agent.web.app.Agent", lambda: FakeAgent(exc))
    page = post(authed, "/assistant", {"question": "hello"}).text
    assert "No Claude API credentials" in page


def test_assistant_conversation(authed, monkeypatch):
    monkeypatch.setattr("grc_agent.web.app.Agent", FakeAgent)
    page = post(authed, "/assistant", {"question": "hello <b>"}).text
    assert "hello &lt;b&gt;" in page and "Hi there" in page and "Tools used: score_risk" in page
    assert "Hi there" not in post(authed, "/assistant/reset").text
