"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from dotenv import find_dotenv, load_dotenv


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 16000
    max_turns: int = 20
    ai_mode: str = "api"  # "api" calls the AI provider; "demo" uses the offline stand-in
    provider: str = "anthropic"  # see grc_agent.llm.PROVIDERS

    @property
    def demo(self) -> bool:
        return self.ai_mode == "demo"

    @property
    def provider_label(self) -> str:
        from grc_agent.llm import PROVIDERS

        return "Demo stand-in" if self.demo else PROVIDERS[self.provider].label

    @classmethod
    def from_env(cls) -> Settings:
        from grc_agent.llm import PROVIDERS, choose_provider

        load_dotenv(find_dotenv(usecwd=True))  # .env in the directory the app runs from
        provider = choose_provider()
        model = os.getenv("GRC_AGENT_MODEL") or PROVIDERS[provider].model or cls.model
        if provider != "anthropic" and model.startswith("claude-"):
            model = PROVIDERS[provider].model  # a Claude model name left over in .env
        return cls(
            model=model,
            effort=os.getenv("GRC_AGENT_EFFORT", cls.effort),
            # Free tiers often cap a reply well below Claude's 16k, so ask for less there.
            max_tokens=int(
                os.getenv("GRC_AGENT_MAX_TOKENS")
                or (cls.max_tokens if provider == "anthropic" else 4096)
            ),
            max_turns=int(os.getenv("GRC_AGENT_MAX_TURNS", cls.max_turns)),
            ai_mode="demo" if os.getenv("GRC_AI_MODE", "").strip().lower() == "demo" else "api",
            provider=provider,
        )


def make_client(settings: Settings) -> Any:
    """The AI client: Claude, an OpenAI-compatible provider, or the offline demo stand-in."""
    if settings.demo:
        from grc_agent.demo import DemoClient

        return DemoClient()
    if settings.provider != "anthropic":
        from grc_agent.llm import PROVIDERS, OpenAICompatClient

        return OpenAICompatClient(PROVIDERS[settings.provider])
    import anthropic

    return anthropic.Anthropic()
