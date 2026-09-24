"""Demo mode: a stand-in for the Claude API, for testing without an API key.

Set GRC_AI_MODE=demo. Every AI feature then runs end to end with scripted,
clearly labelled responses in exactly the shape the real API returns: the
assistant calls the real tools, and assessment drafts are structured JSON that
go through the same citation checks. Nothing here is AI output, so the web app
refuses to deliver an engagement with demo findings.
"""

from __future__ import annotations

import json
import re
from itertools import count
from types import SimpleNamespace
from typing import Any

DEMO_PREFIX = "[Demo mode, not AI]"

_ids = count(1)
_STOP = set(
    "a an and are can do does for how i in is it me my of on or "
    "the to we what which who why with".split()
)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use(name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use", id=f"demo_tool_{next(_ids)}", name=name, input=tool_input
    )


def _response(stop_reason: str, *content: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), stop_details=None)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


class _Messages:
    def create(self, **kwargs: Any) -> SimpleNamespace:
        fmt = (kwargs.get("output_config") or {}).get("format")
        if fmt and fmt.get("type") == "json_schema":
            return self._draft_finding(_content_text(kwargs["messages"][-1]["content"]))
        return self._chat(kwargs["messages"], {t["name"] for t in kwargs.get("tools", [])})

    # -- assessment drafts (ai_assessment.ClaudeAssessor)
    def _draft_finding(self, prompt: str) -> SimpleNamespace:
        provisions = re.findall(r"^\[([^\]]+)\]", prompt, re.M)
        obligation = re.search(r"^OBLIGATION (\S+)[^\n]*\n(.+)$", prompt, re.M)
        status = re.search(r"^STATUS\n(\w[\w ]*)", prompt, re.M)
        ob_id, ob_text = (obligation[1], obligation[2]) if obligation else ("?", "")
        status_word = status[1].split(":")[0].strip().lower() if status else "unknown"
        data = {
            "finding": (
                f"{DEMO_PREFIX} Placeholder finding for {ob_id} ({status_word}): {ob_text} "
                "A real run explains this from the client's answers and the cited provision."
            ),
            "citations": provisions[:1],  # the governing provision is listed first
            "remediation": ""
            if status_word == "compliant"
            else f"{DEMO_PREFIX} Placeholder next step.",
            "confidence": "low",
            "needs_legal_review": False,
        }
        return _response("end_turn", _text(json.dumps(data)))

    # -- assistant chat (agent.Agent)
    def _chat(self, messages: list[dict[str, Any]], tools: set[str]) -> SimpleNamespace:
        last = messages[-1]
        results = [
            b
            for b in (last["content"] if isinstance(last["content"], list) else [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if results:
            return _response("end_turn", _text(self._summarize(results)))

        question = _content_text(last["content"])
        risk = re.search(r"likelihood\D{0,20}(\d)\D+impact\D{0,20}(\d)", question, re.I)
        if risk and "score_risk" in tools:
            return _response(
                "tool_use",
                _text(f"{DEMO_PREFIX} Scoring that risk with the score_risk tool."),
                _tool_use("score_risk", {"likelihood": int(risk[1]), "impact": int(risk[2])}),
            )
        control = re.search(r"\b([A-Z]{2}-\d{2})\b", question)
        if control and "get_control" in tools:
            return _response("tool_use", _tool_use("get_control", {"control_id": control[1]}))
        words = [
            w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in _STOP and len(w) > 2
        ]
        if words and "search_controls" in tools:
            return _response(
                "tool_use",
                _text(f"{DEMO_PREFIX} Searching the control catalog."),
                _tool_use("search_controls", {"query": words[-1], "framework": None}),
            )
        return _response(
            "end_turn",
            _text(
                f'{DEMO_PREFIX} I can only run the tools in demo mode. Try "Score a risk with '
                'likelihood 4 and impact 3" or "Which controls cover MFA?". Add an '
                "ANTHROPIC_API_KEY for real answers."
            ),
        )

    def _summarize(self, results: list[dict[str, Any]]) -> str:
        lines = [f"{DEMO_PREFIX} Here's what the tools returned:"]
        for r in results:
            content = r.get("content", "")
            if r.get("is_error"):
                lines.append(f"- Error: {content}")
                continue
            try:
                data = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                lines.append(f"- {content}")
                continue
            if isinstance(data, dict) and "score" in data:
                lines.append(
                    f"- Risk score {data['score']} (likelihood {data['likelihood']} x impact "
                    f"{data['impact']}): {data['level']}."
                )
            elif isinstance(data, list):
                if not data:
                    lines.append("- No matching controls in the catalog.")
                for c in data[:5]:
                    maps = ", ".join(f"{k} {v}" for k, v in c.get("mappings", {}).items())
                    lines.append(f"- {c['id']} {c['title']}: {c['summary']} ({maps})")
            elif isinstance(data, dict) and "id" in data:
                maps = ", ".join(f"{k} {v}" for k, v in data.get("mappings", {}).items())
                lines.append(f"- {data['id']} {data['title']}: {data['summary']} ({maps})")
            else:
                lines.append(f"- {content}")
        lines.append("Add an ANTHROPIC_API_KEY for real, reasoned answers.")
        return "\n".join(lines)


class DemoClient:
    """Duck-types the parts of anthropic.Anthropic the app uses: client.beta.messages.create."""

    api_key = None

    def __init__(self) -> None:
        self.beta = SimpleNamespace(messages=_Messages())
