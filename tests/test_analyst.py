"""The GRC Analyst: read-only tools over the workspace's data, DPDPA only."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import ALL_YES, PASSWORD, create, login, post

from grc_agent.prompts import ANALYST_PROMPT
from grc_agent.tools import run_tool
from grc_agent.web import cli as web_cli
from grc_agent.web.analyst import analyst_tools
from grc_agent.web.app import create_app


@pytest.fixture
def workspace(authed):
    eid = create(authed)
    answers = {
        **ALL_YES,
        "q_INFO-TOOLS": "Mailchimp, Zoho",
        "q_CTX-FOREIGN": "yes",
        "q_Q-SECURITY": "no",
    }
    post(authed, f"/engagements/{eid}/intake", {**answers, "action": "submit"})
    post(authed, f"/engagements/{eid}/assess", {"mode": "rules"})
    tools = {t.name: t for t in analyst_tools(authed.app)}
    return authed, eid, tools


def call(tools, name, **args):
    content, is_error = run_tool(tools, name, args)
    assert not is_error, content
    return json.loads(content)


def test_tool_set_is_dpdpa_and_read_only(workspace):
    _, _, tools = workspace
    assert set(tools) == {
        "search_obligations",
        "get_provision",
        "score_risk",
        "list_engagements",
        "get_engagement",
        "get_findings",
        "get_risk_register",
        "get_data_flow",
        "get_evidence",
    }
    for t in tools.values():
        spec = t.to_api()
        assert spec["strict"] and spec["input_schema"]["additionalProperties"] is False
    assert "DPDP" in ANALYST_PROMPT and "GDPR" in ANALYST_PROMPT  # scope and refusal both stated


def test_tools_read_the_workspace(workspace):
    _, eid, tools = workspace
    engagements = call(tools, "list_engagements")["engagements"]
    assert engagements[0]["client"] == "Acme Pvt Ltd" and engagements[0]["gaps"] >= 1
    assert engagements[0]["top_risk"]

    eng = call(tools, "get_engagement", engagement_id=eid)
    assert eng["stage"] == "Assessed" and any(
        a["answer"] == "Mailchimp, Zoho" for a in eng["intake"]
    )

    gaps = call(tools, "get_findings", engagement_id=eid, status="gap")["findings"]
    assert {f["status"] for f in gaps} == {"gap"}
    assert any(f["obligation_id"] == "OBL-004" for f in gaps)

    risks = call(tools, "get_risk_register", engagement_id=eid)
    assert risks["summary"]["open"] >= 1
    assert risks["risks"][0]["threat"] and risks["risks"][0]["score"] >= risks["risks"][-1]["score"]

    flow = call(tools, "get_data_flow", engagement_id=eid)
    assert flow["leaves_india"] is True
    assert any("Mailchimp -> Outside India" in f for f in flow["flows"])

    evidence = call(tools, "get_evidence", engagement_id=eid)
    assert evidence["collected_by_connectors"] == []
    assert any(r["obligation_id"] == "OBL-004" for r in evidence["still_to_request"])


def test_unknown_engagement_is_a_tool_error(workspace):
    _, _, tools = workspace
    content, is_error = run_tool(tools, "get_findings", {"engagement_id": 999, "status": "all"})
    assert is_error and "list_engagements" in content


# ---- the page, in demo mode (runs the real tools without an API key)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "corpus"
    shutil.copytree(fixture, tmp_path / "corpus")
    monkeypatch.setenv("GRC_AI_MODE", "demo")
    monkeypatch.setenv("GRC_CORPUS_DIR", str(tmp_path / "corpus"))
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    web_cli.main(["--data-dir", str(tmp_path / "data"), "adduser", "harshit", "--password-stdin"])
    client = TestClient(create_app(tmp_path / "data"))
    login(client)
    eid = create(client)
    answers = {**ALL_YES, "q_Q-SECURITY": "no"}
    post(client, f"/engagements/{eid}/intake", {**answers, "action": "submit"})
    post(client, f"/engagements/{eid}/assess", {"mode": "rules"})
    return client, eid


def test_analyst_page_and_queue(demo):
    client, _ = demo
    page = client.get("/assistant").text
    assert "GRC Analyst" in page and "Analyst queue" in page
    assert "A personal data breach through weak safeguards" not in page  # titles, not threats
    assert "Protect personal data with reasonable security" in page  # the top risk is queued


def test_focus_on_a_client(demo):
    client, eid = demo
    page = post(client, "/assistant/focus", {"focus": str(eid)}).text
    assert f"Summarise ENG-{eid:03d} for management" in page
    page = post(client, "/assistant", {"question": "Summarise this client for management"}).text
    assert "Tools used: get_engagement, get_risk_register" in page
    assert f"ENG-{eid:03d} Acme Pvt Ltd" in page  # shown as the question's focus
    assert "[Focus:" not in page  # the raw prefix is never shown
    assert "open risks" in page


def test_portfolio_and_scope_questions(demo):
    client, _ = demo
    page = post(client, "/assistant", {"question": "What should I work on today?"}).text
    assert "Tools used: list_engagements" in page and "Acme Pvt Ltd (ENG-001)" in page
    page = post(client, "/assistant", {"question": "How does this map to GDPR?"}).text
    assert "DPDP Act and Rules only" in page
