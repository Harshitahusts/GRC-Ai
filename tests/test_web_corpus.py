import io
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ai_assessment import FakeClient, cite_first_provision, good
from test_web import ALL_YES, PASSWORD, create, login, post

from grc_agent.ai_assessment import AssessmentError, ClaudeAssessor
from grc_agent.config import Settings
from grc_agent.corpus import ingest
from grc_agent.web import cli as web_cli
from grc_agent.web.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "corpus"


def make_client(tmp_path, monkeypatch, with_corpus):
    corpus_dir = tmp_path / "corpus"
    if with_corpus:
        shutil.copytree(FIXTURE, corpus_dir)
        ingest(corpus_dir)
    monkeypatch.setenv("GRC_CORPUS_DIR", str(corpus_dir))
    monkeypatch.delenv("GRC_CORPUS_INDEX", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    web_cli.main(["--data-dir", str(tmp_path / "data"), "adduser", "harshit", "--password-stdin"])
    app = create_app(tmp_path / "data")
    client = TestClient(app)
    login(client)
    return app, client


@pytest.fixture
def with_corpus(tmp_path, monkeypatch):
    return make_client(tmp_path, monkeypatch, True)


@pytest.fixture
def without_corpus(tmp_path, monkeypatch):
    return make_client(tmp_path, monkeypatch, False)


def fake_assessor(app, script=cite_first_provision):
    client = FakeClient(script)
    app.state.make_assessor = lambda corpus: ClaudeAssessor(
        corpus, client=client, settings=Settings(model="m", effort="low")
    )
    return client


def submitted(client):
    eid = create(client)
    post(client, f"/engagements/{eid}/intake", {**ALL_YES, "q_Q-BREACH": "no", "action": "submit"})
    return eid


def test_without_corpus_claude_is_off_and_sample_index_is_used(without_corpus):
    app, client = without_corpus
    assert app.state.index_source == "sample index"
    assert "No corpus built yet" in client.get("/corpus").text
    eid = submitted(client)
    page = client.get(f"/engagements/{eid}/findings").text
    assert re.search(r'value="claude"[^>]*disabled', page)
    page = post(client, f"/engagements/{eid}/assess", {"mode": "claude"}).text
    assert "Build the corpus first" in page


def test_corpus_search_and_provision_pages(with_corpus):
    app, client = with_corpus
    assert app.state.index_source == "built corpus"
    page = client.get("/corpus", params={"q": "breach intimation to the Board"}).text
    assert "Section 5(2)" in page and "Ingestion clean" in page
    page = client.get("/corpus/provision", params={"ref": "Section 6(2)"}).text
    assert "targeted advertising" in page and "Resolves in the corpus index" in page
    page = client.get("/corpus/provision", params={"ref": "Section 77"}).text
    assert "treat the citation as fabricated" in page


def test_claude_assessment_end_to_end(with_corpus):
    app, client = with_corpus
    fake = fake_assessor(app)
    eid = submitted(client)
    page = post(client, f"/engagements/{eid}/assess", {"mode": "claude"}).text
    assert fake.calls, "Claude was never called"
    assert "Drafted by Claude" in page and "high confidence" in page
    assert 'href="/corpus/provision?ref=' in page
    record = client.get(f"/engagements/{eid}/export.json").json()
    assert all(f["citations"] for f in record["findings"])
    activity = client.get(f"/engagements/{eid}").text
    assert "assessed with claude" in activity


def test_ungrounded_citations_block_delivery(with_corpus):
    app, client = with_corpus
    fake_assessor(
        app,
        lambda p, n: cite_first_provision(p, n) if "OBL-001" not in p else good(["Section 77(9)"]),
    )
    eid = submitted(client)
    page = post(client, f"/engagements/{eid}/assess", {"mode": "claude"}).text
    assert "✗ unresolved" in page and "Delivery is blocked" in page
    assert "Section 77(9)" in page


def test_failed_claude_run_keeps_previous_findings(with_corpus):
    app, client = with_corpus
    eid = submitted(client)
    post(client, f"/engagements/{eid}/assess", {"mode": "rules"})

    class Broken:
        def assess(self, register, answers):
            raise AssessmentError("OBL-002: unusable output after a retry (not json).")

    app.state.make_assessor = lambda corpus: Broken()
    page = post(client, f"/engagements/{eid}/assess", {"mode": "claude"}).text
    assert "nothing was changed" in page
    assert page.count('class="status-') == 13  # the rule-based findings are still there
