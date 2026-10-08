"""The DPDP readiness plan: a client's obligations grouped into steps, in working order.

Nothing here is stored. Every time the plan is shown it is worked out again from the
engagement's real state (intake, findings, controls, evidence, tasks, delivery checks),
so its progress can't go stale or be ticked off by hand. Each obligation gets one next
action: the single most useful thing to do now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from grc_agent.register import Obligation


@dataclass(frozen=True)
class Theme:
    key: str
    title: str
    why: str
    register: str = ""  # a register that does the day-to-day work, e.g. "breaches"
    register_label: str = ""


THEMES = (
    Theme(
        "consent",
        "Notice and consent",
        "Personal data may only be processed for a lawful purpose, mostly with consent that "
        "follows a clear notice and can be withdrawn as easily as it was given.",
        "consent",
        "Consent register",
    ),
    Theme(
        "children",
        "Children's data",
        "A child's data needs verifiable consent from a parent or guardian, and tracking or "
        "targeted advertising aimed at children is not allowed.",
    ),
    Theme(
        "security",
        "Security and breaches",
        "Reasonable security safeguards are required, and every breach must be reported to "
        "the Board and to the people affected.",
        "breaches",
        "Breach register",
    ),
    Theme(
        "rights",
        "Rights and grievances",
        "People can ask for a summary of their data, have it corrected or erased, and complain; "
        "each request needs an answer within the published period.",
        "requests",
        "Requests register",
    ),
    Theme(
        "retention",
        "Retention and erasure",
        "Data must be erased once its purpose is served or consent is withdrawn, unless a law "
        "requires keeping it.",
    ),
    Theme(
        "processors",
        "Processors and vendors",
        "Any vendor that processes personal data for the client needs a valid contract, and the "
        "client stays responsible for what the vendor does.",
        "vendors",
        "Vendor register",
    ),
    Theme(
        "transfers",
        "Transfers outside India",
        "Personal data can't go to a country the government has restricted.",
    ),
    Theme(
        "accountability",
        "Accountability",
        "Someone must be named to answer questions about the client's processing, with extra "
        "duties if the client is a Significant Data Fiduciary.",
        "policies",
        "Policy register",
    ),
    Theme("other", "Other obligations", "Obligations in the register outside the groups above."),
)
THEME_BY_KEY = {t.key: t for t in THEMES}

_SECTION = re.compile(r"Section\s+(\d+)(?:\((\d+)\))?", re.I)


def theme_of(source: str) -> str:
    """Which step an obligation belongs to, from the provision it comes from."""
    m = _SECTION.search(source or "")
    if not m:
        return "other"
    sec, sub = int(m.group(1)), int(m.group(2) or 0)
    if sec in (4, 5, 6, 7):
        return "consent"
    if sec == 9:
        return "children"
    if sec in (11, 12, 13, 14):
        return "rights"
    if sec == 10:
        return "accountability"
    if sec == 16:
        return "transfers"
    if sec == 8:
        return {
            1: "processors",
            2: "processors",
            4: "security",
            5: "security",
            6: "security",
            7: "retention",
            8: "retention",
            9: "accountability",
            10: "rights",
        }.get(sub, "other")
    return "other"


@dataclass
class Item:
    obligation: Obligation
    control: str  # control status key
    control_label: str
    finding: str | None  # assessment status, or None before the first run
    files: int  # evidence files that count
    flagged: int  # files the AI flagged as off-topic (not counted)
    checks: int  # passing connector checks
    open_tasks: int
    done: bool = False
    action: str = ""
    href: str = ""


@dataclass
class Step:
    key: str
    title: str
    why: str
    items: list[Item] = field(default_factory=list)
    parts: list[tuple[str, bool, str]] = field(default_factory=list)  # (label, done, link)
    register: str = ""
    register_label: str = ""

    @property
    def total(self) -> int:
        return len(self.items) + len(self.parts)

    @property
    def completed(self) -> int:
        return sum(i.done for i in self.items) + sum(ok for _, ok, _ in self.parts)

    @property
    def done(self) -> bool:
        return self.total > 0 and self.completed == self.total

    @property
    def pct(self) -> int:
        return round(100 * self.completed / self.total) if self.total else 0


def next_action(item: Item, base: str) -> tuple[bool, str, str]:
    """(done, what to do next, where) for one obligation."""
    o = item.obligation
    if item.control in ("implemented", "not_applicable"):
        label = "Implemented" if item.control == "implemented" else "Not applicable"
        return True, label, f"{base}/controls#{o.id}"
    if item.finding in ("gap", "open_item") and not item.open_tasks:
        query = urlencode(
            {
                "obligation": o.id,
                "title": f"Close gap: {o.source}",
                "details": o.remediation,
                "priority": {"critical": "urgent", "high": "high"}.get(o.severity, "medium"),
                "source": "Readiness plan",
            }
        )
        return False, "Create a task to close the gap", f"{base}/r/tasks/new?{query}"
    if not item.files and not item.checks:
        if item.flagged:
            return (
                False,
                "Replace the evidence the AI flagged",
                f"{base}/evidence?obligation={o.id}",
            )
        return False, "Upload evidence", f"{base}/evidence?obligation={o.id}"
    if item.open_tasks:
        return False, "Finish the open task", f"{base}/r/tasks?q={o.id}"
    return False, "Mark the control implemented", f"{base}/controls#{o.id}"


def build(
    eid: int,
    controls: list[dict],
    flagged: dict[str, int],
    scope: list[tuple[str, bool, str]],
    delivery: list[tuple[str, bool]],
) -> list[Step]:
    """The steps, in order: scope, one per theme that has obligations, then delivery.

    `controls` is ops_views.controls_for(); obligations the assessment found not to apply
    are left out, since there's nothing to do for them.
    """
    base = f"/engagements/{eid}"
    steps = [
        Step(
            "scope",
            "Scope the client",
            "Know who the client is and what personal data it holds before judging anything.",
            parts=scope,
        )
    ]
    by_theme: dict[str, list[Item]] = {}
    for c in controls:
        if c["assessment"] == "not_applicable":
            continue
        item = Item(
            obligation=c["o"],
            control=c["status"],
            control_label=c["label"],
            finding=c["assessment"],
            files=c["files"],
            flagged=flagged.get(c["o"].id, 0),
            checks=c["checks"],
            open_tasks=c["open_tasks"],
        )
        item.done, item.action, item.href = next_action(item, base)
        by_theme.setdefault(theme_of(c["o"].source), []).append(item)
    for theme in THEMES:
        if theme.key in by_theme:
            steps.append(
                Step(
                    theme.key,
                    theme.title,
                    theme.why,
                    items=by_theme[theme.key],
                    register=theme.register,
                    register_label=theme.register_label,
                )
            )
    steps.append(
        Step(
            "deliver",
            "Review and deliver",
            "A person reviews every AI draft and document, then the pack goes to the client.",
            parts=[(label, ok, f"{base}") for label, ok in delivery],
        )
    )
    return steps


def recommended(steps: list[Step]) -> str | None:
    """The first step with work left, or None when everything is done."""
    return next((s.key for s in steps if not s.done), None)
