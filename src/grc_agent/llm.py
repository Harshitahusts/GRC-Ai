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
    # Models to fall back to, best first, when the chosen one isn't offered (any more).
    # Exact ids or parts of ids; the provider's live model list decides what exists.
    preferred: tuple[str, ...] = ()


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
            "openai/gpt-oss-120b",
            "Free tier, no card: about 30 requests a minute and 1,000 a day. Very fast.",
            "https://console.groq.com/keys",
            ("openai/gpt-oss-120b", "qwen/qwen3", "openai/gpt-oss-20b", "llama"),
        ),
        Provider(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "GEMINI_API_KEY",
            "gemini-flash-latest",  # Google's alias that always points at the current Flash
            "Free tier in Google AI Studio: a few requests a minute, long context.",
            "https://aistudio.google.com/apikey",
            ("gemini-flash-latest", "gemini-3-flash", "gemini-3.1-flash", "flash"),
        ),
        Provider(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            "",  # the free line-up changes weekly: picked from the live list
            "Many ':free' models: 20 requests a minute, 50 a day (1,000 after a $10 top-up).",
            "https://openrouter.ai/keys",
            ("gpt-oss-120b", "nemotron", "gemma", "glm", "qwen", "llama"),
        ),
        Provider(
            "cerebras",
            "Cerebras",
            "https://api.cerebras.ai/v1",
            "CEREBRAS_API_KEY",
            "gpt-oss-120b",
            "Free tier: about 30 requests a minute and a million tokens a day.",
            "https://cloud.cerebras.ai/",
            ("gpt-oss-120b", "qwen", "llama"),
        ),
        Provider(
            "mistral",
            "Mistral",
            "https://api.mistral.ai/v1",
            "MISTRAL_API_KEY",
            "mistral-small-latest",
            "Free 'Experiment' plan: needs a phone number, generous monthly tokens.",
            "https://console.mistral.ai/api-keys",
            ("mistral-small-latest", "mistral-medium-latest", "mistral-large-latest"),
        ),
        Provider(
            "nvidia",
            "NVIDIA NIM",
            "https://integrate.api.nvidia.com/v1",
            "NVIDIA_API_KEY",
            "openai/gpt-oss-120b",
            "Free developer credits for many open models, email sign-up only.",
            "https://build.nvidia.com/",
            ("openai/gpt-oss-120b", "nvidia/nemotron", "qwen", "llama"),
        ),
        Provider(
            "ollama",
            "Ollama (on this computer)",
            "http://localhost:11434/v1",
            "",
            "llama3.1",
            "Free and private: the model runs on your own machine. Needs a good computer.",
            "https://ollama.com/download",
            ("gpt-oss", "qwen", "llama", "gemma", "mistral"),  # whatever you have pulled
        ),
        Provider(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            "OPENAI_API_KEY",
            "gpt-5-mini",
            "Paid.",
            "https://platform.openai.com/api-keys",
            ("gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini"),
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
        # Set when the requested model turned out not to exist and another was used.
        self.model_in_use: str | None = None
        self.replaced: str | None = None

    def list_models(self) -> list[str]:
        """The model ids the provider offers this key right now (GET /models)."""
        self._check_ready()
        data = self._request("GET", f"{self.base_url}/models")
        ids = [str(m.get("id", "")) for m in data.get("data") or [] if isinstance(m, dict)]
        return [i.removeprefix("models/") for i in ids if i]

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
        self._check_ready()
        # A model that was already swapped in for a retired one sticks for this client.
        if not model and not self.model_in_use:
            self.model_in_use = self._pick(None)  # no default: choose from the live list
        model = self.model_in_use or model
        if not model:
            raise anthropic.CredentialsError(
                f"No model chosen for {self.provider.label}, and its model list is empty."
            )

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
        try:
            data = self._post(body)
        except anthropic.NotFoundError:
            # Usually the model was retired (providers do this every few months).
            replacement = self._pick(model)
            if not replacement or replacement == model:
                raise
            body["model"] = replacement
            data = self._post(body)
            self.model_in_use, self.replaced = replacement, model
        return from_openai_response(data, json_only=bool(schema))

    def _check_ready(self) -> None:
        if self.provider.key_env and not self.api_key:
            raise anthropic.CredentialsError(
                f"No API key for {self.provider.label}. Put {self.provider.key_env} in .env."
            )
        if not self.base_url:
            raise anthropic.CredentialsError("Set GRC_LLM_BASE_URL for the custom provider.")
        if self.api_key and not _safe_for_key(self.base_url):
            raise anthropic.CredentialsError(
                f"Refusing to send the API key to {self.base_url} over plain HTTP. "
                "Use an https:// address."
            )

    def _pick(self, unavailable: str | None) -> str | None:
        """A working model from the live list, or None if the list can't be read."""
        try:
            ids = self.list_models()
        except anthropic.APIError:
            return None
        if unavailable in ids:
            return None  # the model exists, so the 404 meant something else
        return pick_model(self.provider, ids)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"{self.base_url}/chat/completions", body)

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider.key == "openrouter":
            headers["X-Title"] = "GRC Flow"
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.request(method, url, json=body, headers=headers)
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


# Ids that are clearly not chat models (speech, embeddings, safety filters, images).
NOT_CHAT = (
    "embed",
    "whisper",
    "tts",
    "guard",
    "rerank",
    "moderation",
    "transcribe",
    "speech",
    "image",
    "audio",
    "aqa",
    "ocr",
    "safety",
)


def pick_model(provider: Provider, ids: list[str], wanted: str = "") -> str | None:
    """The model to use: `wanted` if offered, else the best of `provider.preferred`."""
    if wanted and wanted in ids:
        return wanted
    chat = [i for i in ids if not any(word in i.lower() for word in NOT_CHAT)]
    if provider.key == "openrouter":  # never fall back to a paid model by accident
        chat = [i for i in chat if i.endswith(":free")]
    for pref in provider.preferred:
        exact = [i for i in chat if i == pref]
        part = [i for i in chat if pref in i]
        if exact or part:
            return (exact or part)[0]
    return chat[0] if chat else None


def _safe_for_key(url: str) -> bool:
    """A key may go over HTTPS anywhere, or over plain HTTP only within this network."""
    from grc_agent.web.https import is_local_host

    parts = httpx.URL(url)
    return parts.scheme == "https" or is_local_host(parts.host)


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
