"""Deterministic gap assessment: intake answers -> one finding per obligation.

No model call. Applicability and status come from rules, so the same answers
always produce the same findings. Every obligation gets a finding, including
not_applicable ones, so nothing passes silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from grc_agent.kpis.citations import CorpusIndex
from grc_agent.register import Obligation, Register

SEVERITY_WEIGHTS = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class AssessedFinding:
    obligation_id: str
    status: str  # gap | compliant | open_item | not_applicable
    severity: str
    citation: str
    citation_resolves: bool
    summary: str
    remediation: str


def assess(
    register: Register, answers: dict[str, str], index: CorpusIndex
) -> list[AssessedFinding]:
    return [_assess_one(o, answers, index) for o in register.obligations]


def _assess_one(o: Obligation, answers: dict[str, str], index: CorpusIndex) -> AssessedFinding:
    status, summary = _status(o, answers)
    return AssessedFinding(
        obligation_id=o.id,
        status=status,
        severity=o.severity,
        citation=o.source,
        citation_resolves=index.resolves(o.source),
        summary=summary,
        remediation=o.remediation if status in {"gap", "open_item"} else "",
    )


def _status(o: Obligation, answers: dict[str, str]) -> tuple[str, str]:
    if o.applies_if:
        trigger = answers.get(o.applies_if.question)
        if trigger is None or trigger == "not_sure":
            return "open_item", "Can't tell whether this applies: the client didn't answer clearly."
        if trigger != o.applies_if.equals:
            return "not_applicable", "Doesn't apply, based on the client's answers."

    answer = answers.get(o.question)
    if answer == "yes":
        return "compliant", "Client says this is in place. Check the evidence before relying on it."
    if answer == "no":
        return "gap", "Client says this isn't in place."
    if answer == "not_sure":
        return "open_item", "Client wasn't sure. Follow up on the clarification call."
    return "open_item", "Question skipped. Follow up with the client."


def readiness_score(findings: list[AssessedFinding] | list[dict]) -> int | None:
    """Severity-weighted share of applicable obligations that are compliant (0-100).

    Open items count as not compliant, so skipping questions can't raise the score.
    """

    def get(f, key):
        return f[key] if isinstance(f, dict) else getattr(f, key)

    applicable = [f for f in findings if get(f, "status") != "not_applicable"]
    total = sum(SEVERITY_WEIGHTS[get(f, "severity")] for f in applicable)
    if not total:
        return None
    met = sum(
        SEVERITY_WEIGHTS[get(f, "severity")] for f in applicable if get(f, "status") == "compliant"
    )
    return round(100 * met / total)
