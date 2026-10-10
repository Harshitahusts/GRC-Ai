"""Every duty the DPDP Act 2023 and the DPDP Rules 2025 place on an organisation.

The assessment register (data/dpdpa_register.sample.json) asks about the core
obligations; this catalogue is the full list an organisation tracks to prove compliance:
one item per duty, with where it comes from, who it applies to, the penalty row it falls
under in the Act's Schedule, the date it applies from, and the assessment obligations
(OBL-...) whose evidence and findings speak to it.

Rule numbers follow the Rules as notified (G.S.R. 846(E), November 2025). Check the
Gazette text before relying on a summary: these are plain-English paraphrases.
"""

from dataclasses import dataclass

# The Schedule to the Act (Section 33): maximum penalty per breach, in rupees.
PENALTIES = {
    "security": ("Failure to take reasonable security safeguards (s.8(5))", 250_00_00_000),
    "breach": (
        "Failure to notify the Board and Data Principals of a breach (s.8(6))",
        200_00_00_000,
    ),
    "children": ("Breach of the additional obligations for children (s.9)", 200_00_00_000),
    "sdf": (
        "Breach of a Significant Data Fiduciary's additional obligations (s.10)",
        150_00_00_000,
    ),
    "other": ("Breach of any other provision of the Act or Rules", 50_00_00_000),
}

# Who an item applies to. "all" applies to every Data Fiduciary; the others only when
# the intake (or the organisation) says so.
SCOPES = {
    "all": "Every Data Fiduciary",
    "children": "If you process children's data",
    "processors": "If you use Data Processors (vendors)",
    "cross_border": "If personal data goes outside India",
    "sdf": "Only if notified as a Significant Data Fiduciary",
    "consent_manager": "If you are, or rely on, a Consent Manager",
    "disability": "If you process data of persons with disabilities",
}
# Intake answers that switch a scope on (question id, answer).
SCOPE_QUESTIONS = {
    "children": ("CTX-CHILDREN", "yes"),
    "processors": ("CTX-VENDORS", "yes"),
    "cross_border": ("CTX-FOREIGN", "yes"),
}

MAY_2027 = "13 May 2027"
NOV_2026 = "13 November 2026"


@dataclass(frozen=True)
class Item:
    id: str
    ref: str  # where it comes from, e.g. "Section 8(6), Rule 7"
    title: str
    duty: str  # what the organisation must do, in plain English
    proof: str  # what evidence shows it
    penalty: str  # a key of PENALTIES
    scope: str = "all"  # a key of SCOPES
    applies_from: str = MAY_2027
    links: tuple[str, ...] = ()  # assessment obligations (OBL-...) about the same duty
    register: str = ""  # the register whose records are day-to-day proof, if any


CATALOGUE: tuple[Item, ...] = (
    # ---- the Act: grounds, notice and consent
    Item(
        "A-04",
        "Section 4",
        "Lawful purpose and ground",
        "Process personal data only for a lawful purpose, with consent or for a legitimate use.",
        "Purpose register showing the ground for each purpose.",
        "other",
    ),
    Item(
        "A-05",
        "Section 5(1), Rule 3",
        "Notice before consent",
        "Give a standalone, plain-language notice listing each item of personal data, its "
        "purpose, how to withdraw consent, exercise rights and complain to the Board.",
        "The notice for each channel (web, app, forms).",
        "other",
        links=("OBL-001",),
    ),
    Item(
        "A-05b",
        "Section 5(2)",
        "Notice for consent taken before the Act",
        "Send people whose consent was taken before the Act a notice as soon as practicable.",
        "Copy of the notice sent and the date.",
        "other",
    ),
    Item(
        "A-06",
        "Section 6(1)-(3)",
        "Valid consent",
        "Consent must be free, specific, informed, unconditional and unambiguous, by a clear "
        "affirmative action, for necessary data only, in English or a scheduled language.",
        "Consent screens and consent records.",
        "other",
        links=("OBL-002",),
        register="consent",
    ),
    Item(
        "A-06b",
        "Section 6(4)-(6)",
        "Withdrawing consent",
        "Withdrawal must be as easy as giving consent; stop processing (and have processors "
        "stop) within a reasonable time after it.",
        "Withdrawal flow and records of withdrawals acted on.",
        "other",
        links=("OBL-003",),
        register="consent",
    ),
    Item(
        "A-06c",
        "Section 6(10)",
        "Proving consent",
        "Be able to prove that notice was given and consent obtained.",
        "Timestamped consent log.",
        "other",
        register="consent",
    ),
    Item(
        "A-07",
        "Section 7",
        "Legitimate uses",
        "Where you rely on a legitimate use instead of consent, record which one and why.",
        "Purpose register entries for legitimate uses.",
        "other",
    ),
    # ---- the Act: general duties of a Data Fiduciary
    Item(
        "A-08-1",
        "Section 8(1)",
        "Accountability",
        "You are responsible for compliance, including for processing your processors do for you.",
        "Named owner, policies and this register kept up to date.",
        "other",
    ),
    Item(
        "A-08-2",
        "Section 8(2)",
        "Processors under contract",
        "Engage a Data Processor only under a valid contract.",
        "Signed data processing agreements for each processor.",
        "other",
        scope="processors",
        links=("OBL-012",),
        register="vendors",
    ),
    Item(
        "A-08-3",
        "Section 8(3)",
        "Accuracy",
        "Keep data complete, accurate and consistent when it is used to make a decision about "
        "the person or is disclosed to another Data Fiduciary.",
        "Data quality checks and correction process.",
        "other",
    ),
    Item(
        "A-08-4",
        "Section 8(4)",
        "Technical and organisational measures",
        "Put appropriate technical and organisational measures in place to observe the Act.",
        "Policies, training records, assigned responsibilities.",
        "other",
    ),
    Item(
        "A-08-5",
        "Section 8(5), Rule 6",
        "Reasonable security safeguards",
        "Protect personal data with reasonable security safeguards, at a minimum: encryption, "
        "obfuscation or masking; access control; logging and monitoring; backups and "
        "continuity; logs kept one year; the same safeguards required of processors.",
        "Encryption settings, access reviews, monitoring, backup tests, connector checks.",
        "security",
        links=("OBL-004",),
    ),
    Item(
        "A-08-6",
        "Section 8(6), Rule 7",
        "Breach intimation",
        "On a personal data breach, tell each affected person and the Board without delay, "
        "and send the Board a detailed report within 72 hours.",
        "Breach register with notification times; breach playbook and drill record.",
        "breach",
        links=("OBL-005",),
        register="breaches",
    ),
    Item(
        "A-08-7",
        "Section 8(7), Rule 8",
        "Retention and erasure",
        "Erase personal data once the purpose is served or consent is withdrawn (and have "
        "processors erase it), unless a law requires keeping it.",
        "Retention schedule and erasure log.",
        "other",
        links=("OBL-006",),
        register="erasure",
    ),
    Item(
        "A-08-9",
        "Section 8(9), Rule 9",
        "Contact person published",
        "Publish the business contact of a person (or DPO) who can answer questions about "
        "personal data, on the website or app and in every response to a Data Principal.",
        "Screenshot of the published contact details.",
        "other",
        links=("OBL-007",),
    ),
    Item(
        "A-08-10",
        "Section 8(10)",
        "Grievance redressal",
        "Run an effective way for people to raise grievances.",
        "Grievance channel and grievance records.",
        "other",
        links=("OBL-008",),
        register="requests",
    ),
    # ---- the Act: children
    Item(
        "A-09-1",
        "Section 9(1), Rule 10",
        "Verifiable parental consent",
        "Get verifiable consent of a parent (or lawful guardian) before processing a child's "
        "data, checking the parent is an identifiable adult.",
        "Age gate and parental-consent records.",
        "children",
        scope="children",
        links=("OBL-011",),
    ),
    Item(
        "A-09-2",
        "Section 9(2)-(3)",
        "No harm, tracking or targeted ads for children",
        "Do not process children's data in a way likely to harm them, and do not track, "
        "behaviourally monitor or target advertising at children.",
        "Configuration showing tracking and ad targeting are off for children.",
        "children",
        scope="children",
    ),
    # ---- the Act: Significant Data Fiduciaries
    Item(
        "A-10-a",
        "Section 10(2)(a)",
        "Data Protection Officer in India",
        "Appoint a Data Protection Officer based in India, answerable to the board of "
        "directors, as the point of contact for grievances.",
        "Appointment letter and published DPO details.",
        "sdf",
        scope="sdf",
    ),
    Item(
        "A-10-b",
        "Section 10(2)(b)",
        "Independent data auditor",
        "Appoint an independent data auditor to evaluate compliance.",
        "Auditor engagement letter.",
        "sdf",
        scope="sdf",
    ),
    Item(
        "A-10-c",
        "Section 10(2)(c), Rule 13",
        "Yearly DPIA and audit",
        "Carry out a Data Protection Impact Assessment and an audit every 12 months and send "
        "the Board a report of the significant observations; check that algorithmic software "
        "used to process personal data does not put rights at risk.",
        "DPIA report, audit report, the copy sent to the Board.",
        "sdf",
        scope="sdf",
        register="dpias",
    ),
    # ---- the Act: rights of Data Principals
    Item(
        "A-11",
        "Section 11, Rule 14",
        "Right to information",
        "On request, give a summary of the personal data being processed, the processing, "
        "and who it was shared with.",
        "Request register with responses.",
        "other",
        links=("OBL-009",),
        register="requests",
    ),
    Item(
        "A-12",
        "Section 12, Rule 14",
        "Correction, completion, updating and erasure",
        "Correct, complete, update or erase personal data on request.",
        "Request register with responses.",
        "other",
        links=("OBL-010",),
        register="requests",
    ),
    Item(
        "A-13",
        "Section 13, Rule 14",
        "Responding within the time limit",
        "Publish how to make requests and respond to rights requests and grievances within "
        "the period you publish, at most 90 days.",
        "Published rights page; request register showing response dates.",
        "other",
        register="requests",
    ),
    Item(
        "A-14",
        "Section 14, Rule 14",
        "Nomination",
        "Let people nominate someone to exercise their rights on death or incapacity.",
        "Nomination process and records.",
        "other",
    ),
    # ---- the Act: transfers
    Item(
        "A-16",
        "Section 16, Rule 15",
        "Transfers outside India",
        "Do not transfer personal data to a country the Government restricts, and meet any "
        "requirements the Government sets for transfers.",
        "List of where data is stored and sent; check against restrictions.",
        "other",
        scope="cross_border",
        links=("OBL-013",),
    ),
    # ---- Rules only
    Item(
        "R-04",
        "Rule 4",
        "Consent Managers",
        "If acting as a Consent Manager, register with the Board and meet the First "
        "Schedule's conditions; if relying on one, make sure it is registered.",
        "Registration or the Consent Manager's registration details.",
        "other",
        scope="consent_manager",
        applies_from=NOV_2026,
    ),
    Item(
        "R-08-48h",
        "Rule 8",
        "48-hour notice before erasure",
        "Where the Rules' retention periods apply, tell the person at least 48 hours before "
        "erasing their data, so they can log in or make contact to keep it.",
        "Erasure log showing the notice date and the erasure date.",
        "other",
        register="erasure",
    ),
    Item(
        "R-08-logs",
        "Rule 8, Rule 6",
        "Keep logs for one year",
        "Keep personal data, related traffic data and processing logs for at least one year "
        "for the purposes the Rules list, then erase them unless another law requires them.",
        "Log retention settings.",
        "security",
    ),
    Item(
        "R-11",
        "Rule 11",
        "Persons with disabilities",
        "Before processing data of a person with a disability who has a lawful guardian, "
        "verify the guardian's appointment and get their consent.",
        "Guardian verification records.",
        "other",
        scope="disability",
    ),
    Item(
        "R-23",
        "Rule 23",
        "Information the Government calls for",
        "Provide information the Government calls for under the Seventh Schedule, without "
        "disclosing it where told not to.",
        "Log of information requests and responses.",
        "other",
    ),
)

BY_ID = {i.id: i for i in CATALOGUE}

STATUSES = {
    "not_assessed": "Not assessed",
    "compliant": "Compliant",
    "partial": "Partly compliant",
    "non_compliant": "Not compliant",
    "not_applicable": "Doesn't apply",
}
STATUS_BADGE = {
    "not_assessed": "badge-none",
    "compliant": "badge-pass",
    "partial": "badge-warn",
    "non_compliant": "badge-fail",
    "not_applicable": "badge-none",
}


def crore(rupees: int) -> str:
    """₹250 crore, ₹10,000 ..."""
    if rupees >= 1_00_00_000:
        return f"₹{rupees // 1_00_00_000:,} crore"
    return f"₹{rupees:,}"


def scope_applies(scope: str, answers: dict[str, str]) -> bool | None:
    """True/False when the intake answers it, None when only the organisation knows
    (a Significant Data Fiduciary is notified by the Government, for example)."""
    if scope == "all":
        return True
    if scope in SCOPE_QUESTIONS:
        qid, yes = SCOPE_QUESTIONS[scope]
        answer = answers.get(qid, "")
        if not answer or answer == "not_sure":
            return None
        return answer == yes
    return None


def exposure(items: list[dict]) -> dict:
    """The penalty rows at stake: any applicable item that isn't compliant puts its row's
    maximum in play. Penalties are caps per breach, decided by the Board, so this is the
    worst case per kind of breach, not a forecast."""
    rows: dict[str, dict] = {}
    for it in items:
        if it["status"] in ("compliant", "not_applicable"):
            continue
        key = it["item"].penalty
        label, amount = PENALTIES[key]
        row = rows.setdefault(key, {"label": label, "amount": amount, "duties": []})
        row["duties"].append(it["item"])
    ordered = sorted(rows.values(), key=lambda r: -r["amount"])
    return {
        "rows": ordered,
        "total": sum(r["amount"] for r in ordered),
        "total_label": crore(sum(r["amount"] for r in ordered)),
    }


SEVERITY = {"security": "critical", "breach": "critical", "children": "high", "sdf": "high"}


def as_obligation(item: Item):
    """A catalogue item in the shape of an assessment obligation, so evidence can be linked
    to it and checked by the AI the same way."""
    from grc_agent.register import Obligation

    return Obligation(
        id=item.id,
        question="",
        source=item.ref,
        severity=SEVERITY.get(item.penalty, "medium"),
        obligation=f"{item.title}: {item.duty}",
        evidence=item.proof,
        remediation=item.duty,
    )


AS_OBLIGATIONS = tuple(as_obligation(i) for i in CATALOGUE)
