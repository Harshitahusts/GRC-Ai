"""Tools the agent can call. Each tool is a plain function plus a JSON schema.

To add a tool: write a handler that takes keyword arguments and returns a string
(or a JSON-serializable value), then register it in TOOLS below.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


class ToolError(Exception):
    """Raised by a handler for errors that should be reported back to the model."""


@cache
def load_controls() -> list[dict[str, Any]]:
    text = resources.files("grc_agent.data").joinpath("controls.json").read_text("utf-8")
    return json.loads(text)


def search_controls(query: str, framework: str | None = None) -> list[dict[str, Any]]:
    terms = query.lower().split()
    results = []
    for control in load_controls():
        haystack = " ".join(
            [control["id"], control["title"], control["domain"], control["summary"]]
        ).lower()
        if framework and framework not in control["mappings"]:
            continue
        if all(term in haystack for term in terms):
            results.append(control)
    return results


def get_control(control_id: str) -> dict[str, Any]:
    for control in load_controls():
        if control["id"].lower() == control_id.lower():
            return control
    raise ToolError(f"No control with id {control_id!r}. Use search_controls to find ids.")


RISK_LEVELS = [(20, "critical"), (12, "high"), (6, "medium"), (0, "low")]


def score_risk(likelihood: int, impact: int) -> dict[str, Any]:
    for name, value in (("likelihood", likelihood), ("impact", impact)):
        if not 1 <= value <= 5:
            raise ToolError(f"{name} must be between 1 and 5, got {value}.")
    score = likelihood * impact
    level = next(label for threshold, label in RISK_LEVELS if score >= threshold)
    return {"likelihood": likelihood, "impact": impact, "score": score, "level": level}


FRAMEWORKS = ["ISO27001", "SOC2", "NIST_CSF"]

TOOLS: list[Tool] = [
    Tool(
        name="search_controls",
        description=(
            "Search the internal control catalog by keywords (matched against id, title, "
            "domain, and summary; all terms must match). Optionally restrict to controls "
            "mapped to a framework. Returns a list of matching controls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, e.g. 'access review'."},
                "framework": {
                    "anyOf": [{"type": "string", "enum": FRAMEWORKS}, {"type": "null"}],
                    "description": "Only return controls mapped to this framework, or null.",
                },
            },
            "required": ["query", "framework"],
            "additionalProperties": False,
        },
        handler=search_controls,
    ),
    Tool(
        name="get_control",
        description="Get one control from the internal catalog by its id (e.g. 'AC-02').",
        input_schema={
            "type": "object",
            "properties": {"control_id": {"type": "string"}},
            "required": ["control_id"],
            "additionalProperties": False,
        },
        handler=get_control,
    ),
    Tool(
        name="score_risk",
        description=(
            "Score a risk on a 5x5 matrix. Likelihood and impact are integers 1-5. "
            "Returns score (likelihood * impact) and level: low, medium, high, or critical."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "likelihood": {"type": "integer", "description": "1 (rare) to 5 (almost certain)."},
                "impact": {"type": "integer", "description": "1 (negligible) to 5 (severe)."},
            },
            "required": ["likelihood", "impact"],
            "additionalProperties": False,
        },
        handler=score_risk,
    ),
]


def run_tool(tools: dict[str, Tool], name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
    """Run a tool and return (content, is_error) for a tool_result block."""
    tool = tools.get(name)
    if tool is None:
        return f"Unknown tool {name!r}.", True
    try:
        result = tool.handler(**tool_input)
    except ToolError as exc:
        return str(exc), True
    except TypeError as exc:
        return f"Invalid input for {name}: {exc}", True
    return result if isinstance(result, str) else json.dumps(result), False
