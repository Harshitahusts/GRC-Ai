import json

from grc_agent.tools import TOOLS, get_control, run_tool, score_risk, search_controls

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def test_search_controls_matches_all_terms():
    ids = [c["id"] for c in search_controls("access reviews", framework=None)]
    assert ids == ["AC-03"]


def test_search_controls_filters_by_framework():
    assert search_controls("", framework="SOC2")
    assert search_controls("", framework="PCI") == []


def test_get_control_is_case_insensitive():
    assert get_control("ac-02")["title"] == "Multi-factor authentication"


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
    assert run_tool(TOOLS_BY_NAME, "get_control", {"control_id": "ZZ-99"})[1]
    assert run_tool(TOOLS_BY_NAME, "score_risk", {"likelihood": 9, "impact": 1})[1]
    assert run_tool(TOOLS_BY_NAME, "nope", {})[1]
    assert run_tool(TOOLS_BY_NAME, "get_control", {"wrong": "x"})[1]


def test_tool_schemas_are_strict():
    for tool in TOOLS:
        spec = tool.to_api()
        assert spec["strict"] is True
        assert spec["input_schema"]["additionalProperties"] is False
