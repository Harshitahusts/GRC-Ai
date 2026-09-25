import io
import re

import anthropic
import pytest
from docx import Document
from helpers import ALL_YES, PASSWORD, create, login, post

from grc_agent.web import cli as web_cli
from grc_agent.web.security import hash_password, verify_password

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
    assert 'data-north-star="1"' in kpis

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


def test_assistant_replies_render_markdown_safely(authed, monkeypatch):
    from types import SimpleNamespace

    class MarkdownAgent(FakeAgent):
        def ask(self, question):
            reply = "**Bold** point\n\n- one\n- two\n\n<script>alert(1)</script>"
            self.messages += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": [SimpleNamespace(type="text", text=reply)]},
            ]
            return SimpleNamespace(tool_calls=[])

    monkeypatch.setattr("grc_agent.web.app.Agent", MarkdownAgent)
    page = post(authed, "/assistant", {"question": "hi"}).text
    assert "<strong>Bold</strong>" in page and "<li>one</li>" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_assistant_reply_around_a_tool_call_is_one_bubble(authed, monkeypatch):
    from types import SimpleNamespace

    class ToolAgent(FakeAgent):
        def ask(self, question):
            self.messages += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": [SimpleNamespace(type="text", text="Checking.")]},
                {"role": "user", "content": [{"type": "tool_result"}]},
                {"role": "assistant", "content": [SimpleNamespace(type="text", text="Done.")]},
            ]
            return SimpleNamespace(tool_calls=["score_risk"])

    monkeypatch.setattr("grc_agent.web.app.Agent", ToolAgent)
    page = post(authed, "/assistant", {"question": "hi"}).text
    assert page.count('class="msg msg-assistant"') == 1 and "Checking." in page and "Done." in page


def test_assistant_empty_state_suggests_questions(authed):
    page = authed.get("/assistant").text
    assert "How can I help?" in page and 'data-question="Which controls cover MFA?"' in page


def test_init_creates_first_account_only_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "priya")
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    assert web_cli.main(["--data-dir", str(tmp_path), "init"]) == 0
    assert "Created user 'priya'" in capsys.readouterr().out
    assert web_cli.main(["--data-dir", str(tmp_path), "init"]) == 0
    assert "already exist" in capsys.readouterr().out


def test_dashboard_pipeline_and_attention(authed):
    from helpers import ALL_YES, create, post

    page = authed.get("/").text
    assert "All clear." in page and "No engagements yet." in page
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", {**ALL_YES, "action": "submit"})
    page = authed.get("/").text
    assert "Intake is in. Run the assessment." in page
    assert 'data-tip="Intake submitted: 1 engagement"' in page
    assert "harshit</strong> intake submitted" in page
    assert "</strong> login" not in page  # logins don't crowd the activity list


def test_serve_refuses_a_port_another_copy_is_using(tmp_path, monkeypatch, capsys):
    """An old copy still running would otherwise answer the browser with old code."""
    import socket

    started = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append(k))
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code = web_cli.main(["--data-dir", str(tmp_path), "serve", "--port", str(port), "--open"])
    assert code == 1 and not started and not opened
    err = capsys.readouterr().err
    assert f"Port {port} is already in use" in err and "Ctrl+C" in err

    code = web_cli.main(["--data-dir", str(tmp_path), "serve", "--port", str(port)])
    assert code == 0 and started  # free again: starts normally
