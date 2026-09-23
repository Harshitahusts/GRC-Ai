from types import SimpleNamespace

import pytest

from grc_agent.agent import Agent
from grc_agent.config import Settings


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def response(stop_reason, *content):
    return SimpleNamespace(stop_reason=stop_reason, content=list(content))


class FakeClient:
    """Stands in for anthropic.Anthropic; returns (or raises) scripted responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        next_response = self.responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


def make_agent(responses, **settings):
    client = FakeClient(responses)
    return Agent(client=client, settings=Settings(**settings)), client


def test_runs_tools_then_answers():
    agent, client = make_agent(
        [
            response("tool_use", tool_use("t1", "score_risk", {"likelihood": 4, "impact": 5})),
            response("end_turn", text("That is a critical risk.")),
        ]
    )

    result = agent.ask("Score this risk")

    assert result.text == "That is a critical risk."
    assert result.tool_calls == ["score_risk"]
    assert result.turns == 2
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "t1"
    assert '"level": "critical"' in tool_result["content"]
    assert tool_result["is_error"] is False


def test_request_uses_settings_and_fallbacks():
    agent, client = make_agent([response("end_turn", text("hi"))], model="m", effort="low")
    agent.ask("hello")
    call = client.calls[0]
    assert call["model"] == "m"
    assert call["output_config"] == {"effort": "low"}
    assert call["fallbacks"] == "default"
    assert {t["name"] for t in call["tools"]} == {"search_controls", "get_control", "score_risk"}


def test_conversation_continues_across_asks():
    agent, client = make_agent(
        [
            response("end_turn", text("first")),
            response("end_turn", text("second")),
        ]
    )
    agent.ask("one")
    agent.ask("two")
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "assistant", "user"]


def test_refusal_is_reported():
    agent, _ = make_agent([response("refusal")])
    assert agent.ask("x").stop_reason == "refusal"


def test_stops_at_max_turns():
    loop = [
        response("tool_use", tool_use(f"t{i}", "get_control", {"control_id": "AC-01"}))
        for i in range(3)
    ]
    agent, _ = make_agent(loop, max_turns=3)
    result = agent.ask("loop forever")
    assert result.stop_reason == "max_turns"
    assert result.tool_calls == ["get_control"] * 3


def test_failed_request_rolls_back_history():
    agent, _ = make_agent([response("end_turn", text("ok")), RuntimeError("network down")])
    agent.ask("first")

    with pytest.raises(RuntimeError):
        agent.ask("second")

    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


def test_api_key_from_dotenv_is_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
    assert Agent().client.api_key == "sk-ant-from-dotenv"
