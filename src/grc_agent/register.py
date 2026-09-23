"""The obligation register and intake questions it's assessed from."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low")
CHOICES = {"yes": "Yes", "no": "No", "not_sure": "Not sure"}


@dataclass(frozen=True)
class Condition:
    question: str
    equals: str


@dataclass(frozen=True)
class Question:
    id: str
    type: str  # "choice" (yes / no / not_sure) or "text"
    section: str
    text: str
    help: str = ""
    show_if: Condition | None = None


@dataclass(frozen=True)
class Obligation:
    id: str
    question: str
    source: str
    severity: str
    obligation: str
    evidence: str
    remediation: str
    applies_if: Condition | None = None


@dataclass(frozen=True)
class Register:
    version: str
    notice: str
    questions: tuple[Question, ...]
    obligations: tuple[Obligation, ...]

    def question(self, question_id: str) -> Question:
        return next(q for q in self.questions if q.id == question_id)

    def sections(self) -> list[tuple[str, list[Question]]]:
        out: dict[str, list[Question]] = {}
        for q in self.questions:
            out.setdefault(q.section, []).append(q)
        return list(out.items())


def _condition(data: dict[str, Any] | None) -> Condition | None:
    return Condition(**data) if data else None


def parse_register(data: dict[str, Any]) -> Register:
    questions = tuple(
        Question(**{**q, "show_if": _condition(q.get("show_if"))}) for q in data["questions"]
    )
    obligations = tuple(
        Obligation(**{**o, "applies_if": _condition(o.get("applies_if"))})
        for o in data["obligations"]
    )
    register = Register(data["version"], data.get("notice", ""), questions, obligations)
    _validate(register)
    return register


def _validate(register: Register) -> None:
    ids = {q.id for q in register.questions}
    choice_ids = {q.id for q in register.questions if q.type == "choice"}
    for q in register.questions:
        if q.type not in {"choice", "text"}:
            raise ValueError(f"{q.id}: type must be 'choice' or 'text'")
        if q.show_if and q.show_if.question not in choice_ids:
            raise ValueError(f"{q.id}: show_if refers to unknown choice question")
    seen = set()
    for o in register.obligations:
        if o.id in seen:
            raise ValueError(f"Duplicate obligation id {o.id}")
        seen.add(o.id)
        if o.question not in choice_ids:
            raise ValueError(f"{o.id}: question {o.question!r} is not a choice question")
        if o.applies_if and o.applies_if.question not in choice_ids:
            raise ValueError(f"{o.id}: applies_if refers to unknown choice question")
        if o.severity not in SEVERITIES:
            raise ValueError(f"{o.id}: severity must be one of {', '.join(SEVERITIES)}")
    if not ids:
        raise ValueError("Register has no questions")


@cache
def load_register(path: str | None = None) -> Register:
    """Load the register from GRC_REGISTER, or the bundled sample."""
    path = path or os.getenv("GRC_REGISTER")
    if path:
        text = Path(path).read_text("utf-8")
    else:
        text = resources.files("grc_agent.data").joinpath("dpdpa_register.sample.json").read_text()
    return parse_register(json.loads(text))


def corpus_index_path() -> str:
    return os.getenv("GRC_CORPUS_INDEX") or str(
        resources.files("grc_agent.data").joinpath("corpus_index.sample.txt")
    )
