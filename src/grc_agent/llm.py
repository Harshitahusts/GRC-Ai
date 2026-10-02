"""AI providers other than Claude: any OpenAI-compatible chat API (Groq, Gemini, ...).

The agent and the assessor talk to one interface, `client.beta.messages.create(...)`,
shaped like the Anthropic SDK. `OpenAICompatClient` speaks that interface and translates
each call into an OpenAI-style `/chat/completions` request, then translates the reply
back into Anthropic-shaped content blocks (text and tool_use). Nothing else in the app
needs to know which provider answered.

Errors come back as the Anthropic SDK's own exception classes, so the existing
"bad key", "rate limited" and "API error" handling works for every provider.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    key_env: str  # "" when no key is needed (a model running on this machine)
    model: str  # default model; GRC_AGENT_MODEL overrides it
    free: str  # one line on the free tier, shown on the AI provider page
    signup: str


ANTHROPIC = Provider(
    "anthropic",
    "Anthropic Claude",
    "https://api.anthropic.com",
    "ANTHROPIC_API_KEY",
    "claude-opus-5",
    "Paid. The best results: adaptive thinking and grounded citations.",
    "https://console.anthropic.com/",
)

PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in [
        ANTHROPIC,
        Provider(
            "groq",
            "Groq",
            "https://api.groq.com/openai/v1",
            "GROQ_API_KEY",
            "llama-3.3-70b-versatile",
            "Free tier, no card: about 30 requests a minute and 1,000 a day. Very fast.",
            "https://console.groq.com/keys",
        ),
        Provider(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "GEMINI_API_KEY",
            "gemini-2.5-flash",
            "Free tier in Google AI Studio: a few requests a minute, long context.",
            "https://aistudio.google.com/apikey",
        ),
        Provider(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            "meta-llama/llama-3.3-70b-instruct:free",
            "Many ':free' models: 20 requests a minute, 50 a day (1,000 after a $10 top-up).",
            "https://openrouter.ai/keys",
        ),
        Provider(
            "cerebras",
            "Cerebras",
            "https://api.cerebras.ai/v1",
            "CEREBRAS_API_KEY",
            "gpt-oss-120b",
            "Free tier: about 30 requests a minute and a million tokens a day.",
            "https://cloud.cerebras.ai/",
        ),
        Provider(
            "mistral",
            "Mistral",
            "https://api.mistral.ai/v1",
            "MISTRAL_API_KEY",
            "mistral-small-latest",
            "Free 'Experiment' plan: needs a phone number, generous monthly tokens.",
            "https://console.mistral.ai/api-keys",
        ),
        Provider(
            "nvidia",
            "NVIDIA NIM",
            "https://integrate.api.nvidia.com/v1",
            "NVIDIA_API_KEY",
            "meta/llama-3.3-70b-instruct",
            "Free developer credits for many open models, email sign-up only.",
            "https://build.nvidia.com/",
        ),
        Provider(
            "ollama",
            "Ollama (on this computer)",
            "http://localhost:11434/v1",
            "",
            "llama3.1",
            "Free and private: the model runs on your own machine. Needs a good computer.",
            "https://ollama.com/download",
        ),
        Provider(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            "OPENAI_API_KEY",
            "gpt-4.1-mini",
            "Paid.",
            "https://platform.openai.com/api-keys",
        ),
        Provider(
            "custom",
            "Other OpenAI-compatible API",
            "",  # GRC_LLM_BASE_URL
            "GRC_LLM_API_KEY",
            "",
            "Any server that speaks the OpenAI chat API (LM Studio, vLLM, LiteLLM, ...).",
            "",
        ),
    ]
}

# When GRC_AI_PROVIDER is not set, the first provider with a key in the environment wins.
AUTO_ORDER = [
    "anthropic",
    "groq",
    "gemini",
    "openrouter",
    "cerebras",
    "mistral",
    "nvidia",
    "openai",
]


def choose_provider() -> str:
    """The provider key from GRC_AI_PROVIDER, or the first one that has an API key set."""
    wanted = os.getenv("GRC_AI_PROVIDER", "").strip().lower()
    if wanted:
        if wanted not in PROVIDERS:
            raise SystemExit(
                f"GRC_AI_PROVIDER={wanted!r} is not supported. Use one of: " + ", ".join(PROVIDERS)
            )
        return wanted
    for key in AUTO_ORDER:
        if os.getenv(PROVIDERS[key].key_env, "").strip():
            return key
    return "anthropic"


def key_status() -> list[dict[str, Any]]:
    """For the settings page: which providers have a key. Never returns the key itself."""
    return [
        {
            "provider": p,
            "configured": (not p.key_env) or bool(os.getenv(p.key_env, "").strip()),
        }
        for p in PROVIDERS.values()
    ]


# ------------------------------------------------------------------ the client


class OpenAICompatClient:
    """Duck-types `anthropic.Anthropic().beta.messages.create` over an OpenAI-style API."""

    def __init__(
        self,
        provider: Provider,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        retries: int = 2,
    ) -> None:
        self.provider = provider
        self.api_key = api_key if api_key is not None else os.getenv(provider.key_env, "")
        self.base_url = (base_url or provider.base_url or os.getenv("GRC_LLM_BASE_URL", "")).rstrip(
            "/"
        )
        self.retries = retries
        self._http = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), transport=transport)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = self.beta.messages

    # The Anthropic-only arguments (thinking, cache_control, betas, fallbacks) are accepted
    # and ignored: the other providers have no equivalent.
    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        output_config: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> SimpleNamespace:
        if self.provider.key_env and not self.api_key:
            raise anthropic.CredentialsError(
                f"No API key for {self.provider.label}. Put {self.provider.key_env} in .env."
            )
        if not self.base_url:
            raise anthropic.CredentialsError("Set GRC_LLM_BASE_URL for the custom provider.")

        schema = ((output_config or {}).get("format") or {}).get("schema")
        if schema:
            system = (
                f"{system}\n\nReply with one JSON object only, no other text, matching this "
                f"JSON schema:\n{json.dumps(schema)}"
            )
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": to_openai_messages(system, messages),
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema") or {"type": "object"},
                    },
                }
                for t in tools
            ]
        if schema:
            body["response_format"] = {"type": "json_object"}
        data = self._post(body)
        return from_openai_response(data, json_only=bool(schema))

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider.key == "openrouter":
            headers["X-Title"] = "GRC Agent"
        url = f"{self.base_url}/chat/completions"
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise anthropic.APIConnectionError(
                    message=f"Could not reach {self.provider.label}: {exc.__class__.__name__}",
                    request=httpx.Request("POST", url),
                ) from exc
            # Free tiers rate-limit often: wait briefly and try again before giving up.
            if resp.status_code in (429, 500, 502, 503) and attempt < self.retries:
                time.sleep(_retry_after(resp, attempt))
                continue
            break
        if resp.status_code >= 400:
            raise _status_error(resp, self.provider.label)
        return resp.json()


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "")), 10.0)
    except ValueError:
        return 2.0 * (attempt + 1)


def _status_error(resp: httpx.Response, label: str) -> anthropic.APIStatusError:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:500]
    detail = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("message", detail)
    message = f"{label} returned {resp.status_code}: {str(detail)[:300]}"
    cls = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
        404: anthropic.NotFoundError,
        429: anthropic.RateLimitError,
    }.get(resp.status_code, anthropic.APIStatusError)
    return cls(message, response=resp, body=body)


# ------------------------------------------------------------------ translation


def _get(block: Any, name: str, default: Any = None) -> Any:
    """Content blocks are dicts (built by us) or objects (returned by an SDK)."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "\n".join(_get(b, "text", "") for b in content if _get(b, "type") == "text")
            calls = [
                {
                    "id": _get(b, "id"),
                    "type": "function",
                    "function": {
                        "name": _get(b, "name"),
                        "arguments": json.dumps(_get(b, "input")),
                    },
                }
                for b in content
                if _get(b, "type") == "tool_use"
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # A user turn: tool results become "tool" messages, any text a user message.
        texts = []
        for b in content:
            if _get(b, "type") == "tool_result":
                result = _get(b, "content", "")
                if not isinstance(result, str):
                    result = "\n".join(_get(x, "text", "") for x in result)
                if _get(b, "is_error"):
                    result = f"Error: {result}"
                out.append(
                    {"role": "tool", "tool_call_id": _get(b, "tool_use_id"), "content": result}
                )
            elif _get(b, "type") == "text":
                texts.append(_get(b, "text", ""))
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def from_openai_response(data: dict[str, Any], json_only: bool = False) -> SimpleNamespace:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    blocks: list[Any] = []
    text = (message.get("content") or "").strip()
    if json_only:
        # Some models wrap JSON in a code fence even when asked not to.
        match = FENCE.match(text)
        text = match.group(1) if match else text
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))
    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append(
            SimpleNamespace(
                type="tool_use",
                id=call.get("id") or f"call_{i}",
                name=fn.get("name", ""),
                input=args if isinstance(args, dict) else {},
            )
        )
    finish = choice.get("finish_reason")
    if any(b.type == "tool_use" for b in blocks):
        stop = "tool_use"
    elif finish == "length":
        stop = "max_tokens"
    elif finish == "content_filter":
        stop = "refusal"
    else:
        stop = "end_turn"
    return SimpleNamespace(content=blocks, stop_reason=stop, model=data.get("model"))
