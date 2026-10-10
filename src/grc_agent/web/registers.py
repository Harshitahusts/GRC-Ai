"""Registers: tasks, consent, rights requests, breaches, vendors, DPIAs and policies.

Each register is declared once here: its fields, its status workflow, which statuses
need which fields filled in, how its due date is worked out, and which obligations in
the DPDP register it serves. One set of routes and templates (register_views.py)
serves them all, so every register gets the same list, form, detail page, status
history, comments and evidence attachments.

Deadlines quoted here come from the DPDP Rules 2025: a detailed breach report to the
Board within 72 hours of becoming aware (Rule 7(2)(b)), and a response to Data
Principals' requests and grievances within the period the fiduciary publishes, which
may not exceed 90 days (Rule 14(3)). They are defaults a consultant can shorten.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"  # text | textarea | select | date | datetime | number
    choices: tuple[tuple[str, str], ...] = ()
    required: bool = False
    help: str = ""
    in_list: bool = False  # show as a column in the register table
    maxlen: int = 300


@dataclass(frozen=True)
class Status:
    key: str
    label: str
    tone: str = "todo"  # todo | active | done | bad
    needs: tuple[str, ...] = ()  # fields that must be filled before entering this status
    closed: bool = False


@dataclass(frozen=True)
class RegisterSpec:
    key: str
    title: str
    singular: str
    prefix: str
    icon: str
    group: str
    intro: str  # plain language: what this is and what to do here
    provision: str
    fields: tuple[Field, ...]
    statuses: tuple[Status, ...]
    obligations: tuple[str, ...] = ()
    title_field: str = "title"
    due_label: str = "Due"
    due_from: Callable[[dict], str] | None = None
    note: str = ""  # shown under the form, e.g. what not to enter
    rule: Callable[[str, dict], str | None] | None = None  # extra status checks

    def field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)

    def status(self, key: str) -> Status:
        return next(s for s in self.statuses if s.key == key)

    @property
    def status_keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.statuses)

    @property
    def open_statuses(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.statuses if not s.closed)


def _yes_no(extra: tuple[tuple[str, str], ...] = ()) -> tuple[tuple[str, str], ...]:
    return (("", "Not known"), ("yes", "Yes"), ("no", "No"), *extra)


PRIORITIES = (("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent"))


# ---------------------------------------------------------------- due dates


def _plus_days(value: str, days: int) -> str:
    try:
        return (date.fromisoformat(value[:10]) + timedelta(days=days)).isoformat()
    except ValueError:
        return ""


def _request_due(d: dict) -> str:
    days = d.get("response_days") or "90"
    return _plus_days(d.get("received_on", ""), int(days) if str(days).isdigit() else 90)


def _breach_due(d: dict) -> str:
    """Rule 7(2)(b): the detailed report to the Board is due 72 hours after awareness."""
    try:
        aware = datetime.fromisoformat(d.get("aware_at", ""))
    except ValueError:
        return ""
    return (aware + timedelta(hours=72)).isoformat(timespec="minutes")


def _vendor_rule(status: str, d: dict) -> str | None:
    if status == "active" and d.get("contract") != "signed":
        return (
            "A processor can only be active under a valid contract (Section 8(2)). "
            "Record the signed contract first."
        )
    return None


def _request_rule(status: str, d: dict) -> str | None:
    if status in ("assigned", "in_progress", "resolved") and d.get("identity") != "verified":
        return "Verify the requester's identity before working on the request."
    return None


# ---------------------------------------------------------------- the registers

REGISTERS: dict[str, RegisterSpec] = {}


def _add(spec: RegisterSpec) -> RegisterSpec:
    REGISTERS[spec.key] = spec
    return spec


TASKS = _add(
    RegisterSpec(
        key="tasks",
        title="Tasks",
        singular="task",
        prefix="TSK",
        icon="i-check",
        group="Compliance",
        intro="Every gap becomes a task with an owner and a date. Create them from findings, "
        "risks or inventory gaps, or add your own, then track them to done.",
        provision="Accountability for every obligation",
        fields=(
            Field("title", "What needs doing", required=True, in_list=True, maxlen=160),
            Field("details", "Details", "textarea", maxlen=2000),
            Field("priority", "Priority", "select", PRIORITIES, required=True, in_list=True),
            Field(
                "outcome",
                "What was done",
                "textarea",
                maxlen=1000,
                help="Filled in when the task is done, so an auditor can see the result.",
            ),
        ),
        statuses=(
            Status("todo", "To do"),
            Status("in_progress", "In progress", "active"),
            Status("blocked", "Blocked", "bad"),
            Status("done", "Done", "done", needs=("outcome",), closed=True),
            Status("cancelled", "Cancelled", "done", needs=("outcome",), closed=True),
        ),
    )
)

CONSENT = _add(
    RegisterSpec(
        key="consent",
        title="Consent records",
        singular="consent record",
        prefix="CON",
        icon="i-check",
        group="Privacy operations",
        intro="A log of who agreed to what, under which notice, and when they withdrew. "
        "It lets the client prove consent (Section 6(10)) and honour withdrawals.",
        provision="Sections 6(1), 6(4), 6(10)",
        obligations=("OBL-002", "OBL-003"),
        title_field="principal_ref",
        fields=(
            Field(
                "principal_ref",
                "Data Principal reference",
                required=True,
                in_list=True,
                maxlen=80,
                help="A customer ID or pseudonym, not a name or phone number.",
            ),
            Field("purpose", "Purpose consented to", required=True, in_list=True, maxlen=200),
            Field("notice_version", "Notice version shown", in_list=True, maxlen=40),
            Field(
                "source",
                "Where it was collected",
                "select",
                (
                    ("", "Choose…"),
                    ("web", "Website"),
                    ("app", "Mobile app"),
                    ("paper", "Paper form"),
                    ("call", "Phone call"),
                    ("consent_manager", "Consent Manager"),
                    ("other", "Other"),
                ),
            ),
            Field("granted_at", "Consent given on", "datetime"),
            Field("withdrawn_at", "Withdrawn on", "datetime"),
            Field(
                "basis_note",
                "Other legal basis",
                maxlen=200,
                help="If consent isn't needed, say which legitimate use (Section 7) applies.",
            ),
            Field("evidence_ref", "Proof (log ID, form number)", maxlen=120),
        ),
        statuses=(
            Status("pending", "Pending"),
            Status("granted", "Granted", "done", needs=("granted_at",)),
            Status("withdrawn", "Withdrawn", "bad", needs=("withdrawn_at",), closed=True),
            Status(
                "other_basis", "Not needed: other basis", "done", needs=("basis_note",), closed=True
            ),
        ),
        note="Keep this pseudonymous. Store the Data Principal's details in the client's "
        "own systems, not here.",
    )
)

REQUESTS = _add(
    RegisterSpec(
        key="requests",
        title="Data principal requests",
        singular="request",
        prefix="REQ",
        icon="i-users",
        group="Privacy operations",
        intro="Requests from people about their own data: access, correction, erasure, "
        "grievances and nominations. Each has a clock; nothing closes without a written "
        "resolution.",
        provision="Sections 11-14, Rule 14",
        obligations=("OBL-008", "OBL-009", "OBL-010"),
        title_field="summary",
        due_label="Respond by",
        due_from=_request_due,
        rule=_request_rule,
        fields=(
            Field("summary", "Summary", required=True, in_list=True, maxlen=160),
            Field(
                "request_type",
                "Type",
                "select",
                (
                    ("access", "Access to a summary of their data (s.11)"),
                    ("correction", "Correction, completion or update (s.12)"),
                    ("erasure", "Erasure (s.12(3))"),
                    ("grievance", "Grievance (s.13)"),
                    ("nomination", "Nominate someone (s.14)"),
                    ("withdraw_consent", "Withdraw consent (s.6(4))"),
                    ("other", "Other"),
                ),
                required=True,
                in_list=True,
            ),
            Field(
                "principal_ref",
                "Data Principal reference",
                maxlen=80,
                help="Customer ID or ticket number; avoid names here.",
            ),
            Field(
                "channel",
                "Received through",
                "select",
                (
                    ("", "Choose…"),
                    ("email", "Email"),
                    ("web_form", "Web form"),
                    ("app", "App"),
                    ("phone", "Phone"),
                    ("letter", "Letter"),
                    ("consent_manager", "Consent Manager"),
                ),
            ),
            Field("received_on", "Received on", "date", required=True),
            Field(
                "response_days",
                "Days to respond",
                "number",
                help="The period the client publishes. Rule 14(3) caps it at 90 days.",
            ),
            Field(
                "identity",
                "Identity check",
                "select",
                (
                    ("pending", "Not yet verified"),
                    ("verified", "Verified"),
                    ("failed", "Could not verify"),
                ),
            ),
            Field(
                "resolution",
                "Resolution",
                "textarea",
                maxlen=2000,
                help="What was done and what the person was told.",
            ),
        ),
        statuses=(
            Status("submitted", "Submitted"),
            Status("identity_verification", "Verifying identity", "active"),
            Status("assigned", "Assigned", "active"),
            Status("in_progress", "In progress", "active"),
            Status("resolved", "Resolved", "done", needs=("resolution",)),
            Status("rejected", "Rejected", "bad", needs=("resolution",), closed=True),
            Status("closed", "Closed", "done", needs=("resolution",), closed=True),
        ),
    )
)

BREACHES = _add(
    RegisterSpec(
        key="breaches",
        title="Breaches",
        singular="breach",
        prefix="BRE",
        icon="i-alert",
        group="Privacy operations",
        intro="Log a personal data breach the moment it's known. The clock starts at "
        "awareness: tell affected people without delay and send the Board its detailed "
        "report within 72 hours.",
        provision="Section 8(6), Rule 7",
        obligations=("OBL-005", "OBL-004"),
        due_label="Board report due",
        due_from=_breach_due,
        fields=(
            Field("title", "What happened", required=True, in_list=True, maxlen=160),
            Field(
                "aware_at",
                "Became aware at",
                "datetime",
                required=True,
                help="Starts the 72-hour clock for the Board's detailed report.",
            ),
            Field(
                "breach_type",
                "Type",
                "select",
                (
                    ("unauthorised_access", "Unauthorised access"),
                    ("disclosure", "Unauthorised disclosure"),
                    ("loss", "Loss or theft"),
                    ("alteration", "Alteration"),
                    ("destruction", "Destruction"),
                    ("unavailable", "Loss of access"),
                    ("other", "Other"),
                ),
                in_list=True,
            ),
            Field("affected_count", "People affected (estimate)", "number", in_list=True),
            Field("data_affected", "Data affected", maxlen=300),
            Field("containment", "Containment and mitigation", "textarea", maxlen=2000),
            Field("board_intimated_at", "Board told (initial) at", "datetime"),
            Field("principals_told_at", "Affected people told at", "datetime"),
            Field("board_report_at", "Board detailed report sent at", "datetime"),
            Field("lessons", "Root cause and lessons", "textarea", maxlen=2000),
        ),
        statuses=(
            Status("discovered", "Discovered", "bad"),
            Status("investigating", "Investigating", "active"),
            Status("board_intimated", "Board intimated", "active", needs=("board_intimated_at",)),
            Status(
                "principals_told",
                "People informed",
                "active",
                needs=("board_intimated_at", "principals_told_at"),
            ),
            Status(
                "reported",
                "Board report sent",
                "done",
                needs=("board_intimated_at", "principals_told_at", "board_report_at"),
            ),
            Status(
                "closed",
                "Closed",
                "done",
                needs=("board_intimated_at", "principals_told_at", "board_report_at", "lessons"),
                closed=True,
            ),
        ),
    )
)

VENDORS = _add(
    RegisterSpec(
        key="vendors",
        title="Vendors & processors",
        singular="vendor",
        prefix="VEN",
        icon="i-plug",
        group="Risk",
        intro="Everyone who processes personal data for the client. Each needs a contract "
        "(Section 8(2)), a known data location, and a periodic review.",
        provision="Sections 8(1), 8(2), 16",
        obligations=("OBL-012", "OBL-013"),
        title_field="name",
        due_label="Next review",
        due_from=lambda d: d.get("next_review", ""),
        rule=_vendor_rule,
        fields=(
            Field("name", "Vendor", required=True, in_list=True, maxlen=120),
            Field("service", "Service provided", in_list=True, maxlen=200),
            Field("data_shared", "Personal data shared", maxlen=300),
            Field(
                "location",
                "Where data is processed",
                "select",
                (
                    ("", "Not known"),
                    ("india", "India only"),
                    ("outside", "Outside India"),
                    ("both", "India and abroad"),
                ),
                in_list=True,
            ),
            Field("countries", "Countries (if abroad)", maxlen=120),
            Field(
                "contract",
                "Contract / DPA",
                "select",
                (("none", "None"), ("negotiating", "Negotiating"), ("signed", "Signed")),
                in_list=True,
            ),
            Field(
                "risk",
                "Risk",
                "select",
                (("", "Not assessed"), ("low", "Low"), ("medium", "Medium"), ("high", "High")),
                in_list=True,
            ),
            Field(
                "questionnaire",
                "Security questionnaire",
                "select",
                (
                    ("", "Not sent"),
                    ("sent", "Sent"),
                    ("received", "Received"),
                    ("reviewed", "Reviewed"),
                ),
            ),
            Field("next_review", "Next review", "date"),
            Field("contact", "Vendor contact", maxlen=120),
        ),
        statuses=(
            Status("onboarding", "Onboarding"),
            Status("active", "Active", "done"),
            Status("under_review", "Under review", "active"),
            Status("offboarded", "Offboarded", "done", closed=True),
        ),
    )
)

DPIAS = _add(
    RegisterSpec(
        key="dpias",
        title="Impact assessments (DPIA)",
        singular="DPIA",
        prefix="DPIA",
        icon="i-sliders",
        group="Risk",
        intro="A structured look at a risky processing activity: why it's needed, what "
        "could go wrong for people, and how it's reduced. Required periodically for "
        "Significant Data Fiduciaries (Section 10(2)(c)); good practice for anyone.",
        provision="Section 10(2)(c), Rule 13",
        title_field="activity",
        due_label="Review by",
        due_from=lambda d: d.get("review_by", ""),
        fields=(
            Field("activity", "Processing activity", required=True, in_list=True, maxlen=160),
            Field("description", "What happens to the data", "textarea", maxlen=3000),
            Field("necessity", "Why it's necessary and proportionate", "textarea", maxlen=3000),
            Field("risks", "Risks to Data Principals", "textarea", maxlen=3000),
            Field("mitigations", "Measures that reduce the risk", "textarea", maxlen=3000),
            Field(
                "residual_risk",
                "Risk left after measures",
                "select",
                (("", "Not rated"), ("low", "Low"), ("medium", "Medium"), ("high", "High")),
                in_list=True,
            ),
            Field("reviewer", "Reviewed by", maxlen=80),
            Field("review_by", "Review again by", "date"),
        ),
        statuses=(
            Status("draft", "Draft"),
            Status("in_review", "In review", "active", needs=("risks", "mitigations")),
            Status(
                "approved",
                "Approved",
                "done",
                needs=("risks", "mitigations", "residual_risk", "reviewer"),
            ),
            Status("rework", "Needs rework", "bad"),
        ),
    )
)

POLICIES = _add(
    RegisterSpec(
        key="policies",
        title="Policies",
        singular="policy",
        prefix="POL",
        icon="i-book",
        group="Compliance",
        intro="The client's privacy policies and notices, each with a version, an owner, "
        "an approver and a review date, so nothing goes stale.",
        provision="Section 5 notice; Section 8 duties",
        obligations=("OBL-001",),
        due_label="Next review",
        due_from=lambda d: d.get("next_review", ""),
        fields=(
            Field("title", "Policy", required=True, in_list=True, maxlen=160),
            Field(
                "kind",
                "Type",
                "select",
                (
                    ("notice", "Privacy notice (s.5)"),
                    ("retention", "Retention & erasure"),
                    ("breach", "Breach response"),
                    ("security", "Security safeguards"),
                    ("vendor", "Vendor management"),
                    ("children", "Children's data"),
                    ("grievance", "Grievance redressal"),
                    ("other", "Other"),
                ),
                in_list=True,
            ),
            Field("version", "Version", in_list=True, maxlen=20),
            Field("link", "Where the document lives", maxlen=300),
            Field("approved_by", "Approved by", maxlen=80),
            Field("approved_on", "Approved on", "date"),
            Field("next_review", "Next review", "date"),
            Field("changes", "What changed in this version", "textarea", maxlen=2000),
        ),
        statuses=(
            Status("draft", "Draft"),
            Status("pending_approval", "Awaiting approval", "active", needs=("version",)),
            Status(
                "published", "Published", "done", needs=("version", "approved_by", "approved_on")
            ),
            Status("retired", "Retired", "done", closed=True),
        ),
    )
)


def _erasure_rule(status: str, d: dict) -> str | None:
    """Rule 8(2): where the retention periods in the Rules apply, tell the person at least
    48 hours before erasing, so they can log in or make contact to keep their data."""
    if status != "erased" or d.get("reason") != "inactivity":
        return None
    try:
        told = datetime.fromisoformat(d.get("notice_sent_at", ""))
        erased = datetime.fromisoformat(d.get("erased_at", ""))
    except ValueError:
        return "Record when the 48-hour notice was sent (Rule 8) before marking it erased."
    if erased - told < timedelta(hours=48):
        return "Rule 8 needs the notice at least 48 hours before erasure."
    return None


def _erasure_due(d: dict) -> str:
    """The earliest the data may go: 48 hours after the notice, else the planned date."""
    if d.get("notice_sent_at"):
        try:
            told = datetime.fromisoformat(d["notice_sent_at"])
        except ValueError:
            return d.get("erase_on", "")
        return (told + timedelta(hours=48)).isoformat(timespec="minutes")
    return d.get("erase_on", "")


ERASURE = _add(
    RegisterSpec(
        key="erasure",
        title="Erasure log",
        singular="erasure",
        prefix="ERA",
        icon="i-x",
        group="Privacy operations",
        intro="Personal data due to be erased: purpose served, consent withdrawn, an erasure "
        "request, or three years without contact for the platforms the Rules list. Where "
        "the Rules' periods apply, the person must be told 48 hours before (Rule 8).",
        provision="Section 8(7), Rule 8",
        obligations=("OBL-006",),
        due_label="Erase from",
        due_from=_erasure_due,
        rule=_erasure_rule,
        fields=(
            Field(
                "title",
                "What is being erased",
                required=True,
                in_list=True,
                maxlen=160,
                help="e.g. Inactive customer accounts, 2022 cohort",
            ),
            Field(
                "reason",
                "Why",
                "select",
                (
                    ("purpose_served", "Purpose served (s.8(7)(a))"),
                    ("consent_withdrawn", "Consent withdrawn (s.8(7))"),
                    ("request", "Erasure request (s.12)"),
                    ("inactivity", "No contact for the Rules' period (Rule 8, Third Schedule)"),
                    ("other", "Other"),
                ),
                required=True,
                in_list=True,
            ),
            Field(
                "principal_ref",
                "Data Principal or group reference",
                maxlen=120,
                help="A customer ID, request number or a description of the group.",
            ),
            Field("systems", "Systems it is in", maxlen=300),
            Field("erase_on", "Planned erasure date", "date"),
            Field("notice_sent_at", "48-hour notice sent at", "datetime"),
            Field(
                "processors_told",
                "Processors told to erase too",
                "select",
                _yes_no((("na", "No processors involved"),)),
            ),
            Field("erased_at", "Erased at", "datetime"),
            Field(
                "method",
                "How it was erased",
                maxlen=300,
                help="e.g. Deleted from CRM and backups rotated out after 30 days.",
            ),
            Field(
                "legal_hold",
                "Law that requires keeping it",
                maxlen=300,
                help="Only if it must be kept, e.g. tax records under the Income-tax Act.",
            ),
        ),
        statuses=(
            Status("identified", "Identified"),
            Status("notice_sent", "Notice sent", "active", needs=("notice_sent_at",)),
            Status("erased", "Erased", "done", needs=("erased_at", "method"), closed=True),
            Status("retained", "Kept by law", "done", needs=("legal_hold",), closed=True),
        ),
        note="Don't paste personal data here; use references.",
    )
)

EXCEPTIONS = _add(
    RegisterSpec(
        key="exceptions",
        title="Accepted gaps",
        singular="accepted gap",
        prefix="EXC",
        icon="i-shield",
        group="Compliance",
        intro="A gap you have decided to live with for now, on the record: why, what reduces "
        "the risk meanwhile, who approved it and when it will be looked at again. An "
        "auditor or the Board will ask; this is the answer.",
        provision="Section 8(1) accountability",
        due_label="Review by",
        due_from=lambda d: d.get("review_by", ""),
        fields=(
            Field("title", "The gap", required=True, in_list=True, maxlen=160),
            Field(
                "obligation_ref",
                "Duty it falls short of",
                in_list=True,
                maxlen=80,
                help="e.g. Section 8(5), Rule 6",
            ),
            Field("reason", "Why it is accepted for now", "textarea", required=True, maxlen=2000),
            Field(
                "risk",
                "Risk",
                "select",
                (("", "Not rated"), ("low", "Low"), ("medium", "Medium"), ("high", "High")),
                in_list=True,
            ),
            Field("compensating", "What reduces the risk meanwhile", "textarea", maxlen=2000),
            Field(
                "approved_by",
                "Approved by",
                maxlen=80,
                help="Someone with the authority to accept the risk.",
            ),
            Field("approved_on", "Approved on", "date"),
            Field("review_by", "Review by", "date"),
        ),
        statuses=(
            Status("proposed", "Proposed"),
            Status(
                "approved",
                "Approved",
                "done",
                needs=("approved_by", "approved_on", "review_by", "compensating"),
            ),
            Status("rejected", "Rejected: fix it", "bad", closed=True),
            Status("closed", "Gap closed", "done", closed=True),
        ),
    )
)

SYSTEMS = _add(
    RegisterSpec(
        key="systems",
        title="Systems",
        singular="system",
        prefix="SYS",
        icon="i-database",
        group="Risk",
        intro="Every application, database and tool that holds personal data, with its "
        "owner and the safeguards Rule 6 asks for: encryption or masking, access control, "
        "logging kept a year, and backups.",
        provision="Section 8(5), Rule 6",
        obligations=("OBL-004",),
        title_field="name",
        due_label="Next review",
        due_from=lambda d: d.get("next_review", ""),
        fields=(
            Field("name", "System", required=True, in_list=True, maxlen=120),
            Field("system_owner", "Business owner", in_list=True, maxlen=80),
            Field("purpose", "What it's used for", maxlen=300),
            Field("personal_data", "Personal data in it", maxlen=300),
            Field(
                "location",
                "Where it is hosted",
                "select",
                (
                    ("", "Not known"),
                    ("india", "India"),
                    ("outside", "Outside India"),
                    ("both", "India and abroad"),
                ),
                in_list=True,
            ),
            Field("vendor", "Provider (if a vendor runs it)", maxlen=120),
            Field("encryption", "Encrypted or masked", "select", _yes_no(), in_list=True),
            Field("access_control", "Access limited and reviewed", "select", _yes_no()),
            Field("logging", "Access logged, logs kept 1 year", "select", _yes_no()),
            Field("backups", "Backed up and restore tested", "select", _yes_no()),
            Field("retention", "How long data is kept", maxlen=200),
            Field("next_review", "Next review", "date"),
        ),
        statuses=(
            Status("active", "In use", "done"),
            Status("under_review", "Under review", "active"),
            Status("retired", "Retired", "done", closed=True),
        ),
    )
)

DRILLS = _add(
    RegisterSpec(
        key="drills",
        title="Breach drills",
        singular="drill",
        prefix="DRL",
        icon="i-clock",
        group="Privacy operations",
        intro="Practise a breach before a real one: run a scenario, time how long it takes "
        "to tell the Board and the people affected, and record what to fix. The Board "
        "expects notice without delay and a full report within 72 hours.",
        provision="Section 8(6), Rule 7",
        obligations=("OBL-005",),
        due_label="Date",
        due_from=lambda d: d.get("next_drill", "") or d.get("planned_on", ""),
        fields=(
            Field(
                "title",
                "Scenario",
                required=True,
                in_list=True,
                maxlen=160,
                help="e.g. Laptop with a customer export stolen",
            ),
            Field("planned_on", "Planned for", "date"),
            Field("held_on", "Held on", "date", in_list=True),
            Field("participants", "Who took part", maxlen=300),
            Field(
                "intimation_minutes", "Minutes to a ready Board intimation", "number", in_list=True
            ),
            Field("report_hours", "Hours to a ready detailed report", "number"),
            Field("lessons", "What went well and what to fix", "textarea", maxlen=3000),
            Field("next_drill", "Next drill", "date"),
        ),
        statuses=(
            Status("planned", "Planned"),
            Status("held", "Held", "active", needs=("held_on", "participants")),
            Status("closed", "Closed", "done", needs=("held_on", "lessons"), closed=True),
        ),
    )
)


# ---------------------------------------------------------------- validation


def clean(spec: RegisterSpec, form: dict) -> tuple[dict, list[str]]:
    """Validated field values from a submitted form, and errors to show."""
    data: dict[str, str] = {}
    errors: list[str] = []
    for f in spec.fields:
        v = str(form.get(f.name, "") or "").strip()[: f.maxlen]
        if f.kind == "select" and f.choices and v not in {k for k, _ in f.choices}:
            if v:
                errors.append(f"{f.label}: pick one of the options.")
            v = f.choices[0][0] if f.required else ""
        elif f.kind == "date" and v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            errors.append(f"{f.label}: use a date.")
            v = ""
        elif f.kind == "datetime" and v:
            try:
                # Stored as local wall-clock time, the way the browser's picker shows it.
                v = datetime.fromisoformat(v).replace(tzinfo=None).isoformat(timespec="minutes")
            except ValueError:
                errors.append(f"{f.label}: use a date and time.")
                v = ""
        elif f.kind == "number" and v and not v.isdigit():
            errors.append(f"{f.label}: use a whole number.")
            v = ""
        if f.required and not v:
            errors.append(f"{f.label} is required.")
        data[f.name] = v
    if spec.key == "requests" and data.get("response_days") and int(data["response_days"]) > 90:
        errors.append("Days to respond: Rule 14(3) allows at most 90 days.")
        data["response_days"] = "90"
    return data, errors


def status_problems(spec: RegisterSpec, status: str, data: dict) -> list[str]:
    """Why a record can't move to `status` yet (empty list: it can)."""
    st = spec.status(status)
    missing = [spec.field(n).label for n in st.needs if not data.get(n)]
    problems = [f"Fill in {', '.join(missing)} before marking it “{st.label}”."] if missing else []
    if spec.rule:
        extra = spec.rule(status, data)
        if extra:
            problems.append(extra)
    return problems


def due_for(spec: RegisterSpec, data: dict, given: str = "") -> str:
    if spec.due_from:
        return spec.due_from(data)
    return given


def is_overdue(due: str, status: str, spec: RegisterSpec, now: datetime | None = None) -> bool:
    if not due or status not in spec.open_statuses:
        return False
    now = now or datetime.now()
    try:
        when = datetime.fromisoformat(due if "T" in due else due + "T23:59")
    except ValueError:
        return False
    return when.replace(tzinfo=None) < now.replace(tzinfo=None)


GROUPS = ("Privacy operations", "Compliance", "Risk")
