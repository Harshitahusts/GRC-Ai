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
    ai_mode: str = "api"  # "api" calls Claude; "demo" uses the offline stand-in in demo.py

    @property
    def demo(self) -> bool:
        return self.ai_mode == "demo"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(find_dotenv(usecwd=True))  # .env in the directory the app runs from
        return cls(
            model=os.getenv("GRC_AGENT_MODEL", cls.model),
            effort=os.getenv("GRC_AGENT_EFFORT", cls.effort),
            max_tokens=int(os.getenv("GRC_AGENT_MAX_TOKENS", cls.max_tokens)),
            max_turns=int(os.getenv("GRC_AGENT_MAX_TURNS", cls.max_turns)),
            ai_mode="demo" if os.getenv("GRC_AI_MODE", "").strip().lower() == "demo" else "api",
        )


def make_client(settings: Settings) -> Any:
    """The Claude API client, or the offline stand-in when GRC_AI_MODE=demo."""
    if settings.demo:
        from grc_agent.demo import DemoClient

        return DemoClient()
    import anthropic

    return anthropic.Anthropic()
