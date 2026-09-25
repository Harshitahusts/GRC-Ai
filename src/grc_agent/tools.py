"""Tools the agent can call. Each tool is a plain function plus a JSON schema.

The agent covers India's DPDP Act 2023 and DPDP Rules 2025 only. These base tools
work anywhere (the command line included): the obligations register, the text of
the Act and Rules from the ingested corpus, and 5x5 risk scoring. The web app
adds read-only tools over the workspace's own data (grc_agent.web.analyst).

To add a tool: write a handler that takes keyword arguments and returns a string
(or a JSON-serializable value), then register it in a tool list.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
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
def _register():
    from grc_agent.register import load_register

    return load_register()


def search_obligations(query: str) -> dict[str, Any]:
    """DPDPA obligations in the register matching all keywords (or all, for an empty query)."""
    terms = query.lower().split()
    out = []
    for o in _register().obligations:
        haystack = " ".join([o.id, o.source, o.obligation, o.evidence, o.remediation]).lower()
        if all(t in haystack for t in terms):
            out.append(
                {
                    "id": o.id,
                    "provision": o.source,
                    "severity": o.severity,
                    "obligation": o.obligation,
                    "evidence_to_request": o.evidence,
                    "remediation": o.remediation,
                }
            )
    return {"kind": "obligations", "register": _register().version, "results": out}


def get_provision(ref: str) -> dict[str, Any]:
    """The text of a DPDP Act or Rules provision from the ingested corpus."""
    from grc_agent.corpus import load_corpus

    corpus = load_corpus()
    if corpus is None:
        raise ToolError(
            "The DPDPA corpus isn't built on this computer, so provision text isn't available. "
            "Answer from the obligations register and say the text wasn't checked."
        )
    chunks = corpus.provision(ref)
    if not chunks:
        raise ToolError(f"No provision matches {ref!r}. Use a form like 'Section 8(5)'.")
    return {
        "kind": "provision",
        "ref": ref,
        "text": [{"heading": c.heading, "text": c.text[:4000]} for c in chunks[:6]],
    }


RISK_LEVELS = [(20, "critical"), (12, "high"), (6, "medium"), (0, "low")]


def risk_level(score: int) -> str:
    return next(label for threshold, label in RISK_LEVELS if score >= threshold)


def score_risk(likelihood: int, impact: int) -> dict[str, Any]:
    for name, value in (("likelihood", likelihood), ("impact", impact)):
        if not 1 <= value <= 5:
            raise ToolError(f"{name} must be between 1 and 5, got {value}.")
    score = likelihood * impact
    return {
        "kind": "risk_score",
        "likelihood": likelihood,
        "impact": impact,
        "score": score,
        "level": risk_level(score),
    }


BASE_TOOLS: list[Tool] = [
    Tool(
        name="search_obligations",
        description=(
            "Search the DPDPA obligations register by keywords (all must match; an empty "
            "string returns every obligation). Each result has the obligation id, the "
            "provision it comes from, severity, the evidence to request, and the remediation."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "e.g. 'consent' or ''."}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=search_obligations,
    ),
    Tool(
        name="get_provision",
        description=(
            "Get the text of a DPDP Act 2023 or DPDP Rules 2025 provision, e.g. 'Section 8(5)' "
            "or 'Rule 6'. Use it before quoting or relying on what a provision says."
        ),
        input_schema={
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "e.g. 'Section 16(1)'."}},
            "required": ["ref"],
            "additionalProperties": False,
        },
        handler=get_provision,
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
TOOLS = BASE_TOOLS


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
