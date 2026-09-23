"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 16000
    max_turns: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(find_dotenv(usecwd=True))  # .env in the directory the app runs from
        return cls(
            model=os.getenv("GRC_AGENT_MODEL", cls.model),
            effort=os.getenv("GRC_AGENT_EFFORT", cls.effort),
            max_tokens=int(os.getenv("GRC_AGENT_MAX_TOKENS", cls.max_tokens)),
            max_turns=int(os.getenv("GRC_AGENT_MAX_TURNS", cls.max_turns)),
        )
