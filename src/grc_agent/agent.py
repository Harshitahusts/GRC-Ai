"""The agent loop: send the conversation to Claude, run requested tools, repeat."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic

from grc_agent.config import Settings
from grc_agent.prompts import SYSTEM_PROMPT
from grc_agent.tools import TOOLS, Tool, run_tool

# Server-side refusal fallback: if a safety classifier declines, the API retries
# on Anthropic's recommended fallback model within the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class AgentResult:
    text: str
    stop_reason: str | None
    turns: int
    tool_calls: list[str] = field(default_factory=list)


class Agent:
    """A multi-turn agent. Call `ask()` repeatedly to continue the same conversation."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        settings: Settings | None = None,
        tools: list[Tool] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.client = client or anthropic.Anthropic()
        self.settings = settings or Settings.from_env()
        self.tools = {tool.name: tool for tool in (TOOLS if tools is None else tools)}
        self.system_prompt = system_prompt
        # Append-only history. Assistant turns keep their full content blocks
        # (thinking, tool_use) so they can be replayed unchanged.
        self.messages: list[dict[str, Any]] = []

    def _create(self) -> Any:
        return self.client.beta.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=self.system_prompt,
            tools=[tool.to_api() for tool in self.tools.values()],
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.effort},
            cache_control={"type": "ephemeral"},
            fallbacks="default",
            betas=[FALLBACK_BETA],
        )

    def ask(self, prompt: str) -> AgentResult:
        self.messages.append({"role": "user", "content": prompt})
        tool_calls: list[str] = []

        for turn in range(1, self.settings.max_turns + 1):
            response = self._create()
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return AgentResult(
                    text="The request was declined by the model's safety checks.",
                    stop_reason="refusal",
                    turns=turn,
                    tool_calls=tool_calls,
                )

            if response.stop_reason == "pause_turn":
                # A long server-side turn paused; re-send to let it continue.
                continue

            if response.stop_reason != "tool_use":
                # end_turn, max_tokens, stop_sequence: we're done.
                return AgentResult(
                    text=_text_of(response.content),
                    stop_reason=response.stop_reason,
                    turns=turn,
                    tool_calls=tool_calls,
                )

            # All results for one assistant turn go back in a single user message.
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls.append(block.name)
                content, is_error = run_tool(self.tools, block.name, block.input)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            self.messages.append({"role": "user", "content": results})

        return AgentResult(
            text=f"Stopped after {self.settings.max_turns} turns without finishing.",
            stop_reason="max_turns",
            turns=self.settings.max_turns,
            tool_calls=tool_calls,
        )

    def reset(self) -> None:
        self.messages.clear()


def _text_of(content: list[Any]) -> str:
    return "\n".join(block.text for block in content if block.type == "text").strip()
