"""Deterministic gap assessment: intake answers -> one finding per obligation.

No model call. Applicability and status come from rules, so the same answers
always produce the same findings. Every obligation gets a finding, including
not_applicable ones, so nothing passes silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from grc_agent.citations import CorpusIndex
from grc_agent.register import Obligation, Register

SEVERITY_WEIGHTS = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class AssessedFinding:
    obligation_id: str
    status: str  # gap | compliant | open_item | not_applicable
    severity: str
    citations: tuple[str, ...]
    unresolved: tuple[str, ...]  # citations the verifier couldn't resolve; these block delivery
    summary: str
    remediation: str
    drafted_by: str = "rules"  # rules | claude
    confidence: str = ""  # high | medium | low, when drafted by Claude
    needs_legal_review: bool = False
    provisions: tuple[str, ...] = ()  # provisions shown to Claude

    @property
    def citation(self) -> str:
        return "; ".join(self.citations)

    @property
    def citation_resolves(self) -> bool:
        return bool(self.citations) and not self.unresolved


# How a finding's summary is worded. The status is the same either way; only the voice
# changes: a GRC partner writes about "the client", a company about itself.
WORDING = {
    "client": {
        "unclear": "Can't tell whether this applies: the client didn't answer clearly.",
        "not_applicable": "Doesn't apply, based on the client's answers.",
        "compliant": "Client says this is in place. Check the evidence before relying on it.",
        "gap": "Client says this isn't in place.",
        "not_sure": "Client wasn't sure. Follow up on the clarification call.",
        "skipped": "Question skipped. Follow up with the client.",
    },
    "self": {
        "unclear": "Can't tell whether this applies: the answer wasn't clear.",
        "not_applicable": "Doesn't apply, based on your answers.",
        "compliant": "You said this is in place. Upload evidence that proves it.",
        "gap": "You said this isn't in place.",
        "not_sure": "You weren't sure. Check with the team that runs this.",
        "skipped": "Question skipped. Answer it in the intake.",
    },
}


def assess(
    register: Register, answers: dict[str, str], index: CorpusIndex, audience: str = "client"
) -> list[AssessedFinding]:
    words = WORDING.get(audience, WORDING["client"])
    return [_assess_one(o, answers, index, words) for o in register.obligations]


def _assess_one(
    o: Obligation, answers: dict[str, str], index: CorpusIndex, words: dict[str, str]
) -> AssessedFinding:
    status, summary = _status(o, answers, words)
    return AssessedFinding(
        obligation_id=o.id,
        status=status,
        severity=o.severity,
        citations=(o.source,),
        unresolved=() if index.resolves(o.source) else (o.source,),
        summary=summary,
        remediation=o.remediation if status in {"gap", "open_item"} else "",
    )


def _status(o: Obligation, answers: dict[str, str], words: dict[str, str]) -> tuple[str, str]:
    if o.applies_if:
        trigger = answers.get(o.applies_if.question)
        if trigger is None or trigger == "not_sure":
            return "open_item", words["unclear"]
        if trigger != o.applies_if.equals:
            return "not_applicable", words["not_applicable"]

    answer = answers.get(o.question)
    if answer == "yes":
        return "compliant", words["compliant"]
    if answer == "no":
        return "gap", words["gap"]
    if answer == "not_sure":
        return "open_item", words["not_sure"]
    return "open_item", words["skipped"]


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
