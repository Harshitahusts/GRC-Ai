import json

import pytest

from grc_agent.tools import (
    TOOLS,
    get_provision,
    run_tool,
    score_risk,
    search_obligations,
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def test_only_dpdpa_tools():
    assert set(TOOLS_BY_NAME) == {"search_obligations", "get_provision", "score_risk"}


def test_search_obligations_matches_all_terms():
    results = search_obligations("consent withdraw")["results"]
    assert [o["id"] for o in results] == ["OBL-003", "OBL-006"]  # withdrawal, and erasure on it
    for o in results:
        text = " ".join(str(v) for v in o.values()).lower()
        assert "consent" in text and "withdraw" in text
    everything = search_obligations("")["results"]
    assert len(everything) == 13 and everything[0]["provision"] == "Section 5(1)"
    assert "evidence_to_request" in everything[0]


def test_get_provision_needs_the_corpus(monkeypatch, tmp_path):
    from grc_agent.tools import ToolError

    monkeypatch.setenv("GRC_CORPUS_DIR", str(tmp_path / "none"))
    with pytest.raises(ToolError, match="isn't built"):
        get_provision("Section 8(5)")


def test_score_risk_levels():
    assert score_risk(1, 1)["level"] == "low"
    assert score_risk(2, 3)["level"] == "medium"
    assert score_risk(3, 4)["level"] == "high"
    assert score_risk(5, 4)["level"] == "critical"


def test_run_tool_success_returns_json():
    content, is_error = run_tool(TOOLS_BY_NAME, "score_risk", {"likelihood": 2, "impact": 2})
    assert not is_error
    assert json.loads(content)["score"] == 4


def test_run_tool_reports_errors():
    assert run_tool(TOOLS_BY_NAME, "score_risk", {"likelihood": 9, "impact": 1})[1]
    assert run_tool(TOOLS_BY_NAME, "nope", {})[1]
    assert run_tool(TOOLS_BY_NAME, "search_obligations", {"wrong": "x"})[1]


def test_tool_schemas_are_strict():
    for tool in TOOLS:
        spec = tool.to_api()
        assert spec["strict"] is True
        assert spec["input_schema"]["additionalProperties"] is False
