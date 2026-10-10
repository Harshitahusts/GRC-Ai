"""The DPDP due-diligence questionnaire a vendor (Data Processor) answers by link, and how
its answers become a risk score.

Section 8(2) lets a Data Fiduciary engage a processor only under a valid contract, and
the fiduciary stays responsible for what the processor does (Section 8(1)); Rule 6 asks
for the same safeguards from processors. These questions check exactly that. Each answer
earns points; the total decides the risk rating, and a few answers are serious enough on
their own to make a vendor high risk whatever the total.
"""

from __future__ import annotations

from dataclasses import dataclass

ANSWERS = (("yes", "Yes"), ("partly", "Partly"), ("no", "No"), ("na", "Not applicable"))
ANSWER_POINTS = {"yes": 1.0, "partly": 0.5, "no": 0.0}


@dataclass(frozen=True)
class Question:
    id: str
    section: str
    text: str
    weight: int  # 1 (good practice) to 3 (essential)
    ref: str = ""
    critical: bool = False  # "no" makes the vendor high risk on its own
    help: str = ""


QUESTIONS: tuple[Question, ...] = (
    Question(
        "contract",
        "Contract",
        "Will you sign a data processing agreement that limits "
        "you to processing our personal data only on our instructions?",
        3,
        "Section 8(2)",
        critical=True,
    ),
    Question(
        "purpose",
        "Contract",
        "Do you use our personal data only for the service you "
        "provide to us (never for your own purposes, such as marketing or AI training)?",
        3,
        "Section 8(2)",
        critical=True,
    ),
    Question(
        "subprocessors",
        "Contract",
        "Do you tell us before adding or changing sub-processors that handle our data?",
        2,
        "Section 8(2)",
    ),
    Question(
        "encryption",
        "Security safeguards",
        "Is our personal data encrypted in transit and at rest?",
        3,
        "Rule 6",
        critical=True,
    ),
    Question(
        "access",
        "Security safeguards",
        "Is access to our data limited to staff who "
        "need it, with multi-factor authentication and regular access reviews?",
        3,
        "Rule 6",
    ),
    Question(
        "logging",
        "Security safeguards",
        "Do you log access to our data and keep the logs for at least one year?",
        2,
        "Rule 6",
    ),
    Question(
        "backups",
        "Security safeguards",
        "Do you back up our data and test restoring it?",
        2,
        "Rule 6",
    ),
    Question(
        "testing",
        "Security safeguards",
        "Do you run vulnerability scans or penetration tests at least once a year?",
        2,
        "Rule 6",
    ),
    Question(
        "training",
        "Security safeguards",
        "Are your staff trained on data protection at least once a year?",
        1,
        "Section 8(4)",
    ),
    Question(
        "breach",
        "Breaches",
        "Will you tell us about a personal data breach affecting "
        "our data within 24 hours of becoming aware of it?",
        3,
        "Section 8(6), Rule 7",
        critical=True,
        help="We must tell the Board and affected people without delay, so we need to "
        "hear from you fast.",
    ),
    Question(
        "breach_plan",
        "Breaches",
        "Do you have a written incident response plan that you have tested?",
        2,
        "Rule 7",
    ),
    Question(
        "erasure",
        "Retention and rights",
        "Will you erase or return our personal data when the contract ends or when we ask?",
        3,
        "Section 8(7)",
        critical=True,
    ),
    Question(
        "rights",
        "Retention and rights",
        "Can you help us answer people's requests to "
        "access, correct or erase their data within our deadlines?",
        2,
        "Sections 11-13",
    ),
    Question(
        "children",
        "Retention and rights",
        "If our data includes children's data, do "
        "you avoid tracking, behavioural monitoring and targeted advertising?",
        2,
        "Section 9",
    ),
    Question(
        "audit",
        "Assurance",
        "Will you let us (or an auditor) check your compliance, "
        "or share an independent audit report?",
        2,
        "Section 8(1)",
    ),
)

SECTIONS: tuple[tuple[str, tuple[Question, ...]], ...] = tuple(
    (name, tuple(q for q in QUESTIONS if q.section == name))
    for name in dict.fromkeys(q.section for q in QUESTIONS)
)

LOCATIONS = (
    ("india", "India only"),
    ("both", "India and other countries"),
    ("outside", "Outside India only"),
)
CERTIFICATIONS = (
    ("iso27001", "ISO/IEC 27001"),
    ("iso27701", "ISO/IEC 27701"),
    ("soc2", "SOC 2 Type II"),
    ("cert_in", "CERT-In empanelled audit"),
    ("pci", "PCI DSS"),
)


def score(answers: dict[str, str], certifications: list[str]) -> dict:
    """Points earned out of points possible (N/A questions don't count), a rating, and
    the reasons behind it."""
    got = possible = 0.0
    concerns: list[str] = []
    critical_no: list[str] = []
    for q in QUESTIONS:
        a = answers.get(q.id, "")
        if a == "na":
            continue
        possible += q.weight
        got += q.weight * ANSWER_POINTS.get(a, 0.0)
        if a in ("no", ""):
            concerns.append(q.text)
            if q.critical:
                critical_no.append(q.text)
        elif a == "partly" and q.critical:
            concerns.append(f"Only partly: {q.text}")
    # Independent certification is evidence, so it nudges the score up a little.
    bonus = min(len(set(certifications)), 2) * 2.5
    pct = min(100, round(100 * got / possible + bonus)) if possible else 0
    if critical_no or pct < 60:
        risk = "high"
    elif pct < 80:
        risk = "medium"
    else:
        risk = "low"
    return {"pct": pct, "risk": risk, "concerns": concerns, "critical": critical_no}
