"""Compute the North Star and pilot KPIs from engagement records.

Thresholds are the pilot thresholds from the build plan, Step 7. Only
agent-assisted engagements count toward the KPIs; manual engagements are
the baseline for consultant hours.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

from grc_agent.kpis.citations import CorpusIndex
from grc_agent.kpis.models import Engagement

HOURS_TARGET = 6.0
DRAFT_PACK_MINUTES_TARGET = 20.0


@dataclass(frozen=True)
class KpiResult:
    key: str
    name: str
    target: str
    value: float | None
    display: str
    passed: bool | None  # None means no data yet
    detail: str = ""


@dataclass(frozen=True)
class EngagementStatus:
    id: str
    client: str
    verified: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class Scorecard:
    verified_engagements: int
    agent_engagements: int
    engagements: tuple[EngagementStatus, ...]
    kpis: tuple[KpiResult, ...]
    baseline_hours: float | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_kpis_passing(self) -> bool:
        return all(k.passed for k in self.kpis)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_scorecard(engagements: list[Engagement], index: CorpusIndex) -> Scorecard:
    agent = [e for e in engagements if e.mode == "agent"]
    manual_hours = [
        e.consultant_hours
        for e in engagements
        if e.mode == "manual" and e.completed and e.consultant_hours is not None
    ]
    baseline = mean(manual_hours) if manual_hours else None

    statuses = tuple(_engagement_status(e, index) for e in agent)
    warnings = []
    if baseline is None:
        warnings.append(
            "No completed manual engagement with consultant_hours recorded, so there is no "
            "baseline to compare against. Record the next manual engagements."
        )

    return Scorecard(
        verified_engagements=sum(s.verified for s in statuses),
        agent_engagements=len(agent),
        engagements=statuses,
        kpis=(
            _fabricated_citations(agent, index),
            _citation_resolution(agent, index),
            _obligation_coverage(agent),
            _findings_correct(agent),
            _consultant_hours(agent, baseline),
            _full_rewrites(agent),
            _intake_unaided(agent),
            _time_to_draft_pack(agent),
        ),
        baseline_hours=baseline,
        warnings=tuple(warnings),
    )


def _engagement_status(e: Engagement, index: CorpusIndex) -> EngagementStatus:
    """North Star test: is this a Verified Engagement Delivered?"""
    blockers = []
    if not e.completed:
        blockers.append("not completed")
    if not e.findings:
        blockers.append("no findings recorded")
    uncited = sum(1 for f in e.findings if not f.citations)
    if uncited:
        blockers.append(f"{uncited} finding(s) without a citation")
    unresolved = sum(len(index.check(f.citations).unresolved) for f in e.findings)
    if unresolved:
        blockers.append(f"{unresolved} unresolved citation(s)")
    if not e.documents:
        blockers.append("no documents recorded")
    unreviewed = sum(1 for d in e.documents if not d.reviewed)
    if unreviewed:
        blockers.append(f"{unreviewed} document(s) not reviewed")
    rewrites = sum(1 for d in e.documents if d.outcome == "full_rewrite")
    if e.fell_back_to_manual or rewrites:
        reason = f"{rewrites} document(s) fully rewritten" if rewrites else "marked as fallback"
        blockers.append(f"fell back to manual ({reason})")
    return EngagementStatus(e.id, e.client, not blockers, tuple(blockers))


def _no_data(key: str, name: str, target: str, detail: str) -> KpiResult:
    return KpiResult(key, name, target, None, "no data", None, detail)


def _pct(numerator: int, denominator: int) -> float:
    return numerator / denominator


def _fmt_pct(value: float) -> str:
    return f"{value:.0%}" if value in (0, 1) else f"{value:.1%}"


def _citations(agent: list[Engagement]) -> list[str]:
    return [c for e in agent for f in e.findings for c in f.citations]


def _fabricated_citations(agent: list[Engagement], index: CorpusIndex) -> KpiResult:
    key, name, target = "fabricated_citations", "Fabricated citations", "0 absolute"
    citations = _citations(agent)
    if not citations:
        return _no_data(key, name, target, "No citations recorded yet.")
    bad = index.check(citations).unresolved
    detail = "Examples: " + ", ".join(sorted(set(bad))[:5]) if bad else ""
    return KpiResult(key, name, target, float(len(bad)), str(len(bad)), not bad, detail)


def _citation_resolution(agent: list[Engagement], index: CorpusIndex) -> KpiResult:
    key, name, target = "citation_resolution_rate", "Citation resolution rate", "100%"
    citations = _citations(agent)
    if not citations:
        return _no_data(key, name, target, "No citations recorded yet.")
    resolved = len(index.check(citations).resolved)
    rate = _pct(resolved, len(citations))
    detail = f"{resolved} of {len(citations)} citations resolve"
    return KpiResult(key, name, target, rate, _fmt_pct(rate), resolved == len(citations), detail)


def _obligation_coverage(agent: list[Engagement]) -> KpiResult:
    key, name, target = "obligation_coverage", "Obligation coverage", "100% of register"
    measured = [e for e in agent if e.register_size and e.findings]
    if not measured:
        return _no_data(key, name, target, "No engagement has register_size and findings.")

    def coverage(e: Engagement) -> float:
        return min(1.0, len({f.obligation_id for f in e.findings}) / e.register_size)

    worst = min(measured, key=coverage)
    value = coverage(worst)
    detail = f"Lowest: {worst.id} ({_fmt_pct(value)}) across {len(measured)} engagement(s)"
    return KpiResult(key, name, target, value, _fmt_pct(value), value >= 1.0, detail)


def _findings_correct(agent: list[Engagement]) -> KpiResult:
    key, name, target = "findings_correct", "Findings judged correct", ">= 90%"
    findings = [f for e in agent for f in e.findings]
    scored = [f for f in findings if f.verdict]
    if not scored:
        return _no_data(key, name, target, "No findings have a verdict yet.")
    counts = {v: sum(f.verdict == v for f in scored) for v in ("correct", "incomplete", "wrong")}
    rate = _pct(counts["correct"], len(scored))
    detail = (
        f"{counts['correct']} correct, {counts['incomplete']} incomplete, {counts['wrong']} wrong"
        f"; {len(findings) - len(scored)} unscored"
    )
    hallucinations = sum(f.hallucination for f in findings)
    if hallucinations:
        detail += f"; {hallucinations} hallucination(s) logged"
    return KpiResult(key, name, target, rate, _fmt_pct(rate), rate >= 0.9, detail)


def _consultant_hours(agent: list[Engagement], baseline: float | None) -> KpiResult:
    key, name, target = "consultant_hours", "Consultant hours per engagement", "< 6"
    hours = [e.consultant_hours for e in agent if e.completed and e.consultant_hours is not None]
    base = f"Manual baseline: {baseline:.1f} h" if baseline is not None else "No manual baseline"
    if not hours:
        return _no_data(key, name, target, f"No completed engagement with hours. {base}.")
    value = mean(hours)
    detail = f"Mean of {len(hours)} engagement(s). {base}."
    return KpiResult(key, name, target, value, f"{value:.1f} h", value < HOURS_TARGET, detail)


def _full_rewrites(agent: list[Engagement]) -> KpiResult:
    key, name, target = "full_rewrite_rate", "Documents needing full rewrite", "< 10%"
    reviewed = [d for e in agent for d in e.documents if d.reviewed]
    if not reviewed:
        return _no_data(key, name, target, "No reviewed documents yet.")
    rewrites = sum(d.outcome == "full_rewrite" for d in reviewed)
    rate = _pct(rewrites, len(reviewed))
    detail = f"{rewrites} of {len(reviewed)} reviewed documents"
    return KpiResult(key, name, target, rate, _fmt_pct(rate), rate < 0.1, detail)


def _intake_unaided(agent: list[Engagement]) -> KpiResult:
    key, name, target = "intake_unaided_rate", "Intake completion without help", ">= 70%"
    known = [e.intake_completed_unaided for e in agent if e.intake_completed_unaided is not None]
    if not known:
        return _no_data(key, name, target, "intake_completed_unaided not recorded yet.")
    rate = _pct(sum(known), len(known))
    detail = f"{sum(known)} of {len(known)} engagements"
    return KpiResult(key, name, target, rate, _fmt_pct(rate), rate >= 0.7, detail)


def _time_to_draft_pack(agent: list[Engagement]) -> KpiResult:
    key, name, target = "minutes_to_draft_pack", "Intake submit to draft pack", "< 20 min"
    minutes = [m for e in agent if (m := e.minutes_to_draft_pack) is not None]
    if not minutes:
        return _no_data(key, name, target, "No engagement has both timestamps yet.")
    value = mean(minutes)
    detail = f"Mean of {len(minutes)}; slowest {max(minutes):.0f} min"
    passed = value < DRAFT_PACK_MINUTES_TARGET
    return KpiResult(key, name, target, value, f"{value:.0f} min", passed, detail)


def _status(passed: bool | None) -> str:
    return {True: "PASS", False: "FAIL"}.get(passed, "----")


def render_text(card: Scorecard) -> str:
    lines = [
        "NORTH STAR  Verified Engagements Delivered: "
        f"{card.verified_engagements} of {card.agent_engagements} agent engagement(s)",
        "",
    ]
    for s in card.engagements:
        mark = "verified" if s.verified else "not verified: " + "; ".join(s.blockers)
        lines.append(f"  {s.id} ({s.client}): {mark}")
    if card.engagements:
        lines.append("")

    lines.append("PILOT KPIs")
    width = max(len(k.name) for k in card.kpis)
    for k in card.kpis:
        lines.append(
            f"  [{_status(k.passed)}] {k.name:<{width}}  {k.display:>8}  (target {k.target})"
        )
        if k.detail:
            lines.append(f"         {'':<{width}}  {k.detail}")

    for warning in card.warnings:
        lines += ["", f"WARNING: {warning}"]
    return "\n".join(lines)
