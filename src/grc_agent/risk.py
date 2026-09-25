"""The DPDPA risk register for an engagement.

Risk = likelihood x impact (1-5 each, the 5x5 matrix). Each open problem becomes a
risk with a threat (what could happen), a vulnerability (what's missing), a
starting score, and a treatment. Sources:

- findings: every gap or open item against a DPDPA obligation,
- connector evidence: every failed or warning check.

Starting scores follow the rules, not guesswork: a gap is likely (4), an open
item possible (3); impact comes from the obligation's severity. The consultant
can change the scores and record the treatment (mitigate, accept, transfer,
avoid), an owner, a due date and the status; those edits are stored separately
and survive re-assessment.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from grc_agent.tools import risk_level

TREATMENTS = {
    "mitigate": "Mitigate: fix it",
    "accept": "Accept: live with it (record why)",
    "transfer": "Transfer: contract or insure it away",
    "avoid": "Avoid: stop the processing",
}
STATUSES = {"open": "Open", "in_progress": "In progress", "closed": "Closed"}
LEVEL_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
IMPACT_BY_SEVERITY = {"critical": 5, "high": 4, "medium": 3, "low": 2}

# What goes wrong under each part of the Act, by the provision an obligation comes from.
_THREATS = (
    (r"^Section 9\b", "Children's data processed without verifiable parental consent"),
    (r"^Section 5\b", "People aren't told what data is collected or why"),
    (r"^Section 6\b", "Processing without valid, freely given consent"),
    (r"^Section 8\(2\)", "A processor mishandles data with no binding contract"),
    (r"^Section 8\(5\)", "A personal data breach through weak safeguards"),
    (r"^Section 8\(6\)", "A breach goes unreported to the Board and affected people"),
    (r"^Section 8\(7\)", "Personal data kept after its purpose ends"),
    (r"^Section 8\((9|10)\)", "Complaints go unanswered and escalate to the Board"),
    (r"^Section 1[1-4]\b", "Rights requests (access, correction, erasure) are refused or ignored"),
    (r"^Section 16\b", "Personal data transferred to a restricted country"),
)


def threat_for(provision: str) -> str:
    for pattern, threat in _THREATS:
        if re.search(pattern, provision or ""):
            return threat
    return "Non-compliance with the DPDP Act"


@dataclass
class Risk:
    key: str
    title: str
    threat: str
    vulnerability: str
    provision: str
    source: str  # finding | evidence
    link: str
    likelihood: int
    impact: int
    treatment: str = "mitigate"
    action: str = ""
    owner: str = ""
    due: str = ""
    status: str = "open"
    notes: str = ""
    edited: bool = False

    @property
    def score(self) -> int:
        return self.likelihood * self.impact

    @property
    def level(self) -> str:
        return risk_level(self.score)

    def overdue(self, today: date | None = None) -> bool:
        if not self.due or self.status == "closed":
            return False
        try:
            return date.fromisoformat(self.due) < (today or date.today())
        except ValueError:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "score": self.score, "level": self.level, "overdue": self.overdue()}


def build(
    eid: int,
    findings: list[dict[str, Any]],
    obligations: dict[str, Any],
    evidence: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> list[Risk]:
    risks: list[Risk] = []
    for f in findings:
        if f["status"] not in ("gap", "open_item"):
            continue
        o = obligations.get(f["obligation_id"])
        provision = getattr(o, "source", "") or f.get("citation", "")
        gap = f["status"] == "gap"
        risks.append(
            Risk(
                key=f"finding:{f['obligation_id']}",
                title=getattr(o, "obligation", f["obligation_id"]),
                threat=threat_for(provision),
                vulnerability=(
                    f["summary"] if gap else f"Not confirmed yet: {f['summary']}"
                ).strip(),
                provision=provision,
                source="finding",
                link=f"/engagements/{eid}/findings#f{f['id']}",
                likelihood=4 if gap else 3,
                impact=IMPACT_BY_SEVERITY.get(f["severity"], 3),
                action=f["remediation"] or getattr(o, "remediation", ""),
            )
        )
    for e in evidence:
        if e["status"] not in ("fail", "warn"):
            continue
        provision = ", ".join(e["provisions"])
        risks.append(
            Risk(
                key=f"evidence:{e['connector']}:{e['check_key']}",
                title=e["title"],
                threat=threat_for(e["provisions"][0] if e["provisions"] else ""),
                vulnerability=e["detail"],
                provision=provision,
                source="evidence",
                link=f"/engagements/{eid}/connectors",
                likelihood=4 if e["status"] == "fail" else 3,
                impact=4 if any(p.startswith("Section 8(5)") for p in e["provisions"]) else 3,
                action="Fix this setting in the client's account, then run the checks again.",
            )
        )
    for r in risks:
        o = overrides.get(r.key)
        if not o:
            continue
        r.edited = True
        for name in ("likelihood", "impact", "treatment", "owner", "due", "status", "notes"):
            if o.get(name) not in (None, ""):
                setattr(r, name, o[name])
    risks.sort(key=lambda r: (r.status == "closed", -r.score, LEVEL_ORDER[r.level], r.key))
    return risks


def heatmap(risks: list[Risk]) -> list[list[dict[str, Any]]]:
    """Open risks per matrix cell, impact 5 at the top, likelihood 1-5 left to right."""
    rows = []
    for impact in range(5, 0, -1):
        row = []
        for likelihood in range(1, 6):
            here = [
                r
                for r in risks
                if r.status != "closed" and (r.likelihood, r.impact) == (likelihood, impact)
            ]
            row.append(
                {
                    "likelihood": likelihood,
                    "impact": impact,
                    "level": risk_level(likelihood * impact),
                    "count": len(here),
                    "titles": [r.title for r in here],
                }
            )
        rows.append(row)
    return rows


def summary(risks: list[Risk]) -> dict[str, Any]:
    live = [r for r in risks if r.status != "closed"]
    return {
        "open": len(live),
        "closed": len(risks) - len(live),
        "by_level": {lvl: sum(1 for r in live if r.level == lvl) for lvl in LEVEL_ORDER},
        "overdue": sum(1 for r in live if r.overdue()),
        "unowned": sum(1 for r in live if not r.owner),
        "accepted": sum(1 for r in live if r.treatment == "accept"),
    }
