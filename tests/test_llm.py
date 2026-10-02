"""Free and other OpenAI-compatible AI providers, and the AI provider page.

No network: every provider reply comes from an httpx MockTransport or a stub.
"""

import json

import anthropic
import httpx
import pytest
from helpers import post

from grc_agent.agent import Agent
from grc_agent.ai_assessment import drafted_by
from grc_agent.config import Settings, make_client
from grc_agent.llm import (
    PROVIDERS,
    OpenAICompatClient,
    _status_error,
    choose_provider,
    from_openai_response,
    pick_model,
)
from grc_agent.tools import Tool
from grc_agent.web import db as webdb

GROQ = PROVIDERS["groq"]


def reply(content=None, tool_calls=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"model": "m", "choices": [{"message": message, "finish_reason": finish}]}


def fake(*replies, status=200, seen=None):
    queue = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        return httpx.Response(status, json=queue.pop(0) if queue else {"error": {"message": "x"}})

    return httpx.MockTransport(handle)


def test_agent_runs_a_tool_round_trip_on_an_openai_compatible_api():
    seen = []
    transport = fake(
        reply(
            None,
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                }
            ],
            "tool_calls",
        ),
        reply("2 + 3 = 5."),
        seen=seen,
    )
    client = OpenAICompatClient(GROQ, api_key="gsk_test", transport=transport)
    add = Tool("add", "Add two numbers.", {"type": "object"}, lambda a, b: a + b)
    agent = Agent(client=client, settings=Settings(provider="groq", model="llama"), tools=[add])

    result = agent.ask("What is 2 + 3?")

    assert result.text == "2 + 3 = 5." and result.tool_calls == ["add"]
    first, second = seen[0]["body"], seen[1]["body"]
    assert seen[0]["headers"]["authorization"] == "Bearer gsk_test"
    assert first["model"] == "llama" and first["messages"][0]["role"] == "system"
    assert first["tools"][0]["function"]["name"] == "add"
    assert "thinking" not in first and "betas" not in first  # Anthropic-only, dropped
    # The tool call and its result go back in OpenAI's shape.
    assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "add"
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "5"}


def test_json_output_is_requested_and_unfenced():
    seen = []
    client = OpenAICompatClient(
        GROQ, api_key="k", transport=fake(reply('```json\n{"ok": true}\n```'), seen=seen)
    )
    response = client.beta.messages.create(
        model="m",
        max_tokens=100,
        system="Assess.",
        messages=[{"role": "user", "content": "go"}],
        output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}},
    )
    assert json.loads(response.content[0].text) == {"ok": True}
    body = seen[0]["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON schema" in body["messages"][0]["content"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, anthropic.AuthenticationError),
        (404, anthropic.NotFoundError),
        (429, anthropic.RateLimitError),
        (500, anthropic.APIStatusError),
    ],
)
def test_errors_use_the_sdk_exception_classes(status, error):
    client = OpenAICompatClient(GROQ, api_key="k", transport=fake(status=status), retries=0)
    with pytest.raises(error, match=f"Groq returned {status}"):
        client.beta.messages.create(model="m", max_tokens=5, messages=[])


def test_rate_limit_is_retried(monkeypatch):
    monkeypatch.setattr("grc_agent.llm.time.sleep", lambda s: None)
    calls = []

    def handle(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        return httpx.Response(200, json=reply("fine"))

    client = OpenAICompatClient(GROQ, api_key="k", transport=httpx.MockTransport(handle))
    response = client.beta.messages.create(model="m", max_tokens=5, messages=[])
    assert response.content[0].text == "fine" and len(calls) == 2


def test_missing_key_and_unreachable_server():
    with pytest.raises(anthropic.CredentialsError, match="GROQ_API_KEY"):
        OpenAICompatClient(GROQ, api_key="").beta.messages.create(
            model="m", max_tokens=5, messages=[]
        )

    def down(request):
        raise httpx.ConnectError("refused")

    ollama = OpenAICompatClient(PROVIDERS["ollama"], transport=httpx.MockTransport(down))
    with pytest.raises(anthropic.APIConnectionError, match="Ollama"):
        ollama.beta.messages.create(model="m", max_tokens=5, messages=[])


def test_stop_reasons():
    assert from_openai_response(reply("cut", finish="length")).stop_reason == "max_tokens"
    assert from_openai_response(reply("", finish="content_filter")).stop_reason == "refusal"
    assert from_openai_response(reply("done")).stop_reason == "end_turn"


def test_provider_is_picked_from_the_environment(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert choose_provider() == "anthropic"  # nothing set: the old behaviour
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    settings = Settings.from_env()
    assert settings.provider == "gemini" and settings.model == PROVIDERS["gemini"].model
    assert isinstance(make_client(settings), OpenAICompatClient)
    monkeypatch.setenv("GRC_AI_PROVIDER", "groq")
    monkeypatch.setenv("GRC_AGENT_MODEL", "claude-opus-5")  # left over from Claude days
    assert Settings.from_env().model == GROQ.model
    monkeypatch.setenv("GRC_AI_PROVIDER", "nope")
    with pytest.raises(SystemExit):
        Settings.from_env()


def test_findings_record_which_provider_drafted_them():
    assert drafted_by(Settings(provider="groq")) == "groq"
    assert drafted_by(Settings()) == "claude"
    assert drafted_by(Settings(ai_mode="demo", provider="groq")) == "demo"


# ---------------------------------------------------------------- the AI provider page


class FakeProvider:
    """Answers OpenAI-style calls: GET /models lists `models`; a chat with a model not
    in the list gets a 404, like a retired model does."""

    def __init__(self, models):
        self.models = models
        self.keys = []
        self.chat_models = []

    def __call__(self, client, method, url, body=None):
        self.keys.append(client.api_key)
        if method == "GET":
            return {"data": [{"id": m} for m in self.models]}
        self.chat_models.append(body["model"])
        if body["model"] not in self.models:
            response = httpx.Response(404, request=httpx.Request(method, url), json={})
            raise _status_error(response, client.provider.label)
        return reply("OK")


@pytest.fixture
def stub_provider(monkeypatch):
    fake = FakeProvider(["openai/gpt-oss-120b", "openai/gpt-oss-20b", "whisper-large-v3"])
    monkeypatch.setattr(OpenAICompatClient, "_request", lambda client, *a: fake(client, *a))
    return fake


def test_admin_switches_to_a_free_provider(authed, app, stub_provider):
    page = authed.get("/settings/ai").text
    assert "Groq" in page and "Get a key" in page

    r = post(authed, "/settings/ai", {"provider": "groq", "api_key": "gsk_secret_value_1234"})
    # Saving tests the connection straight away.
    assert "now uses Groq" in r.text and "Connected to Groq (openai/gpt-oss-120b)" in r.text
    with webdb.connect(app.state.db_path) as conn:
        row = conn.execute("SELECT * FROM ai_providers").fetchone()
    assert row["active"] == 1 and "gsk_secret" not in row["key_enc"]
    assert row["key_hint"] == "••••1234" and row["status"] == "ok"
    assert "whisper-large-v3" in row["models_json"]
    page = authed.get("/settings/ai").text
    assert "gsk_secret_value_1234" not in page and "••••1234" in page
    assert '<option value="openai/gpt-oss-20b">' in page  # model suggestions
    assert set(stub_provider.keys) == {"gsk_secret_value_1234"}

    assert "Connected to Groq" in post(authed, "/settings/ai/test").text

    # The analyst now answers through Groq.
    r = post(authed, "/assistant", {"question": "Hello?"})
    assert "OK" in r.text

    # Back to .env: the key stays saved but is no longer used.
    post(authed, "/settings/ai/off")
    assert app.state.ai.provider == "anthropic"
    post(authed, "/settings/ai/groq/forget")
    with webdb.connect(app.state.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_providers").fetchone()[0] == 0


def test_a_retired_model_is_swapped_for_a_live_one(authed, app, stub_provider):
    r = post(
        authed,
        "/settings/ai",
        {"provider": "groq", "api_key": "gsk_k_1234", "model": "llama-3.3-70b-versatile"},
    )
    assert "no longer offers llama-3.3-70b-versatile" in r.text
    assert "switched to openai/gpt-oss-120b" in r.text
    assert app.state.ai.model == "openai/gpt-oss-120b"  # remembered, not just this once
    with webdb.connect(app.state.db_path) as conn:
        assert conn.execute("SELECT model FROM ai_providers").fetchone()[0] == "openai/gpt-oss-120b"


def test_env_only_setup_is_tested_and_fixed_too(authed, app, stub_provider, monkeypatch):
    from grc_agent.web import ai_views

    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_env_9876")
    monkeypatch.setenv("GRC_AI_PROVIDER", "groq")  # another test may leave an Anthropic key set
    monkeypatch.setenv("GRC_AGENT_MODEL", "llama-3.3-70b-versatile")
    ai_views.refresh(app)
    assert app.state.ai.provider == "groq"
    r = post(authed, "/settings/ai/test")
    assert "switched to openai/gpt-oss-120b" in r.text and "Connected" in r.text
    assert app.state.ai.model == "openai/gpt-oss-120b"
    assert set(stub_provider.keys) == {"gsk_from_env_9876"}  # still the .env key


def test_the_analyst_survives_a_model_retired_mid_session(stub_provider):
    client = OpenAICompatClient(GROQ, api_key="k")
    agent = Agent(client=client, settings=Settings(provider="groq", model="gone-model"), tools=[])
    assert agent.ask("Hi").text == "OK"
    assert stub_provider.chat_models == ["gone-model", "openai/gpt-oss-120b"]
    agent.ask("Again")  # goes straight to the live model now
    assert stub_provider.chat_models[-1] == "openai/gpt-oss-120b"


def test_no_default_model_picks_from_the_live_list(monkeypatch):
    fake = FakeProvider(["paid/model", "z-ai/glm-5:free", "nvidia/nemotron-3-super:free"])
    monkeypatch.setattr(OpenAICompatClient, "_request", lambda client, *a: fake(client, *a))
    client = OpenAICompatClient(PROVIDERS["openrouter"], api_key="k")
    client.beta.messages.create(model="", max_tokens=5, messages=[])
    assert fake.chat_models == ["nvidia/nemotron-3-super:free"]  # preferred, and free


@pytest.mark.parametrize(
    ("provider", "ids", "wanted", "expected"),
    [
        ("groq", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], "", "openai/gpt-oss-120b"),
        ("groq", ["openai/gpt-oss-20b", "x"], "x", "x"),
        ("groq", ["whisper-large-v3", "llama-guard-4", "qwen/qwen3.6-27b"], "", "qwen/qwen3.6-27b"),
        (
            "gemini",
            ["text-embedding-004", "gemini-3-flash", "gemini-flash-latest"],
            "",
            "gemini-flash-latest",
        ),
        ("openrouter", ["openai/gpt-5", "google/gemma-4-31b:free"], "", "google/gemma-4-31b:free"),
        ("openrouter", ["openai/gpt-5"], "", None),  # never a paid model
        ("ollama", ["mistral:7b"], "", "mistral:7b"),
        ("groq", [], "", None),
    ],
)
def test_pick_model(provider, ids, wanted, expected):
    assert pick_model(PROVIDERS[provider], ids, wanted) == expected


def test_saving_a_provider_switches_demo_mode_off(app, authed, stub_provider):
    app.state.demo = True
    post(authed, "/settings/ai", {"provider": "ollama", "model": "openai/gpt-oss-20b"})
    assert app.state.demo is False and app.state.ai.model == "openai/gpt-oss-20b"
    assert "Demo mode" not in authed.get("/assistant").text


def test_provider_page_validation(authed):
    r = post(authed, "/settings/ai", {"provider": "groq"})
    assert "Paste your Groq API key" in r.text
    r = post(authed, "/settings/ai", {"provider": "custom", "api_key": "k"})
    assert "address of your OpenAI-compatible server" in r.text
    r = post(authed, "/settings/ai", {"provider": "ollama", "base_url": "ftp://x"})
    assert "must start with http" in r.text
    assert post(authed, "/settings/ai", {"provider": "skynet"}).status_code == 400


def test_only_admins_change_the_provider(authed, app):
    with webdb.connect(app.state.db_path) as conn:
        conn.execute("UPDATE users SET role = 'member'")
    assert "Only an admin" in authed.get("/settings/ai").text
    r = post(authed, "/settings/ai", {"provider": "ollama"})
    assert r.status_code == 403
