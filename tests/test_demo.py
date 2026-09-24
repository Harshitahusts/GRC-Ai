import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import ALL_YES, PASSWORD, create, login, post

from grc_agent.agent import Agent
from grc_agent.ai_assessment import ClaudeAssessor
from grc_agent.config import Settings, make_client
from grc_agent.corpus import ingest, load_corpus
from grc_agent.demo import DEMO_PREFIX, DemoClient
from grc_agent.register import load_register
from grc_agent.web import cli as web_cli
from grc_agent.web.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "corpus"
DEMO = Settings(ai_mode="demo")


def test_mode_switch(monkeypatch):
    monkeypatch.setenv("GRC_AI_MODE", "demo")
    assert Settings.from_env().demo
    assert isinstance(make_client(Settings.from_env()), DemoClient)
    monkeypatch.setenv("GRC_AI_MODE", "api")
    assert not Settings.from_env().demo


@pytest.mark.parametrize(
    "question, tool, expected",
    [
        ("Score a risk with likelihood 4 and impact 3", "score_risk", "Risk score 12"),
        ("Which controls cover MFA?", "search_controls", "AC-02 Multi-factor authentication"),
        ("Tell me about AC-03", "get_control", "AC-03 Access reviews"),
    ],
)
def test_assistant_runs_real_tools(question, tool, expected):
    result = Agent(settings=DEMO).ask(question)
    assert result.tool_calls == [tool]
    assert result.text.startswith(DEMO_PREFIX) and expected in result.text


def test_assistant_keeps_conversation():
    agent = Agent(settings=DEMO)
    agent.ask("Which controls cover MFA?")
    agent.ask("Score a risk with likelihood 2 and impact 2")
    assert [m["role"] for m in agent.messages].count("user") == 4  # 2 questions + 2 tool results


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    corpus_dir = tmp_path_factory.mktemp("c") / "corpus"
    shutil.copytree(FIXTURE, corpus_dir)
    ingest(corpus_dir)
    return load_corpus(corpus_dir / "build")


def test_assessment_drafts_are_labelled_and_verified(corpus):
    findings = ClaudeAssessor(corpus, settings=DEMO).assess(load_register(), {"Q-NOTICE": "no"})
    drafted = [f for f in findings if f.drafted_by != "rules"]
    assert drafted and all(f.drafted_by == "demo" for f in drafted)
    assert all(f.summary.startswith(DEMO_PREFIX) for f in drafted)
    assert all(f.citations and f.confidence == "low" for f in drafted)
    # Citations still go through the real checks: each is a provision the stand-in was shown.
    assert all(f.citation_resolves for f in drafted)


def test_demo_output_is_valid_json_for_the_schema():
    response = DemoClient().beta.messages.create(
        messages=[
            {
                "role": "user",
                "content": "OBLIGATION OBL-1 (x)\nDo it.\n\nSTATUS\nGAP: no\n\n"
                "PROVISIONS (cite only these)\n[Section 5(1)] Notice\ntext",
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": {}}},
    )
    data = json.loads(response.content[0].text)
    assert data["citations"] == ["Section 5(1)"] and "OBL-1" in data["finding"]


@pytest.fixture
def demo_app(tmp_path, monkeypatch):
    corpus_dir = tmp_path / "corpus"
    shutil.copytree(FIXTURE, corpus_dir)
    ingest(corpus_dir)
    monkeypatch.setenv("GRC_AI_MODE", "demo")
    monkeypatch.setenv("GRC_CORPUS_DIR", str(corpus_dir))
    monkeypatch.delenv("GRC_CORPUS_INDEX", raising=False)
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    web_cli.main(["--data-dir", str(tmp_path / "data"), "adduser", "harshit", "--password-stdin"])
    client = TestClient(create_app(tmp_path / "data"))
    login(client)
    return client


def test_web_banner_assistant_and_delivery_block(demo_app):
    page = demo_app.get("/").text
    assert "Demo mode: AI answers and drafted findings are simulated" in page

    page = post(demo_app, "/assistant", {"question": "Which controls cover MFA?"}).text
    assert "AC-02 Multi-factor authentication" in page and "Tools used: search_controls" in page

    eid = create(demo_app)
    post(demo_app, f"/engagements/{eid}/intake", {**ALL_YES, "action": "submit"})
    page = post(demo_app, f"/engagements/{eid}/assess", {"mode": "claude"}).text
    assert "Demo placeholder" in page
    overview = demo_app.get(f"/engagements/{eid}").text
    assert "No demo-mode (placeholder) findings" in overview
    page = post(demo_app, f"/engagements/{eid}/deliver").text
    assert "No demo-mode (placeholder) findings" in page and "Can&#39;t deliver yet" in page


def test_no_banner_for_logged_out_visitors(demo_app):
    public = TestClient(demo_app.app)
    assert "Demo mode" not in public.get("/login").text
