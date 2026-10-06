import io

from docx import Document

from grc_agent.assessment import assess
from grc_agent.citations import CorpusIndex
from grc_agent.documents import (
    DOCUMENT_TYPES,
    DPA_BANNER,
    DRAFT_BANNER,
    SELF_BANNER,
    EngagementFacts,
    _lines,
    build_document,
    to_docx,
)
from grc_agent.register import corpus_index_path, load_register

REGISTER = load_register()


def facts():
    answers = {"INFO-DATA": "Names, phone numbers", "Q-NOTICE": "no", "CTX-CHILDREN": "no"}
    findings = assess(REGISTER, answers, CorpusIndex.from_file(corpus_index_path()))
    return EngagementFacts(
        "Acme Pvt Ltd", "SaaS", answers, [{**f.__dict__, "citation": f.citation} for f in findings]
    )


def test_every_document_type_builds_and_exports():
    for kind in DOCUMENT_TYPES:
        blocks = build_document(kind, facts(), REGISTER)
        assert blocks[0].kind == "h1" and "Acme Pvt Ltd" in blocks[0].text
        text = "\n".join(p.text for p in Document(io.BytesIO(to_docx(blocks))).paragraphs)
        assert (DPA_BANNER if kind == "dpa" else DRAFT_BANNER) in text


def test_gap_report_lists_gaps_with_citations_but_not_passes():
    blocks = build_document("gap_report", facts(), REGISTER)
    rows = [row for b in blocks if b.kind == "table" for row in b.rows]
    ids = {row[0] for row in rows}
    assert "OBL-001" in ids  # gap
    assert "OBL-011" not in ids  # not applicable
    assert any(row[3] == "Section 5(1)" for row in rows)


def test_intake_answers_flow_into_documents():
    blocks = build_document("privacy_notice", facts(), REGISTER)
    bullets = [i for b in blocks if b.kind == "bullets" for i in b.items]
    assert "Names" in bullets and "phone numbers" in bullets
    assert any("[Not provided]" in b.text for b in blocks if b.kind == "p")


def test_free_text_splits_into_readable_bullets():
    text = (
        "Students (many aged 10-17): name, class, device ID and IP. "
        "Parents: name, phone, email; Tutors, staff"
    )
    assert _lines(text) == [
        "Students (many aged 10-17): name, class, device ID and IP",
        "Parents: name, phone, email",
        "Tutors",
        "staff",
    ]
    # Commas inside brackets never split an item.
    assert _lines("Cloud hosting (servers, database), a payment gateway") == [
        "Cloud hosting (servers, database)",
        "a payment gateway",
    ]


def records_facts(audience="client"):
    base = facts()
    inventory = [
        {
            "source_name": "CRM",
            "field": "dob",
            "kind": "DATE_OF_BIRTH",
            "risk": "medium",
            "children": 1,
            "purpose": "Run classes",
            "principals": "Students",
            "legal_basis": "consent",
            "retention": "3 years",
            "storage_location": "Mumbai DB",
            "recipients": "Video platform",
            "owner": "Academics",
        },
        {
            "source_name": "CRM",
            "field": "aadhaar",
            "kind": "IN_AADHAAR",
            "risk": "high",
            "children": 0,
            "purpose": "",
            "principals": "Customers",
            "legal_basis": "",
            "retention": "",
            "storage_location": "",
            "recipients": "",
            "owner": "",
        },
    ]
    vendors = [
        {
            "ref": "VEN-001",
            "name": "Video platform",
            "service": "Classes",
            "data_shared": "Video",
            "location": "outside",
            "countries": "United States",
            "contract": "signed",
        },
        {
            "ref": "VEN-002",
            "name": "Chat vendor",
            "service": "Reminders",
            "data_shared": "Mobile",
            "location": "both",
            "countries": "",
            "contract": "none",
        },
    ]
    return EngagementFacts(
        base.client,
        base.sector,
        {**base.answers, "CTX-CHILDREN": "yes"},
        base.findings,
        audience=audience,
        inventory=inventory,
        vendors=vendors,
    )


def test_ropa_is_built_from_the_inventory_and_vendor_register():
    blocks = build_document("ropa", records_facts(), REGISTER)
    headings = [b.text for b in blocks if b.kind == "h3"]
    assert headings == ["PA-01: Run classes", "PA-02: Purpose not recorded yet"]
    cells = " ".join(c for b in blocks if b.kind == "table" for row in b.rows for c in row)
    assert "Date of birth (dob)" in cells and "Video platform" in cells
    assert "Outside India · United States" in cells
    gaps = next(b for b in blocks if b.kind == "bullets").items
    assert "CRM · aadhaar: no purpose, lawful ground, retention period, owner." in gaps
    assert "VEN-002 Chat vendor: no signed contract." in gaps
    assert not any("Consultant" in b.text for b in blocks)


def test_dpa_and_notice_use_the_registers():
    dpa = build_document("dpa", records_facts(), REGISTER)
    assert "Video platform, Chat vendor" in dpa[4].text
    assert [b.text for b in dpa if b.kind == "h3"] == [
        "Schedule VEN-001: Video platform",
        "Schedule VEN-002: Chat vendor",
    ]
    notice = build_document("privacy_notice", records_facts(), REGISTER)
    items = [i for b in notice if b.kind == "bullets" for i in b.items]
    assert "Students: Date of birth" in items and "Classes (Outside India · United States)" in items
    assert any(b.text == "Children" for b in notice if b.kind == "h2")


def test_breach_playbook_states_the_rule_7_timeline():
    blocks = build_document("breach_playbook", facts(), REGISTER)
    text = " ".join([b.text for b in blocks] + [i for b in blocks for i in b.items])
    assert "within 72 hours" in text and "CERT-In" in text and "Consultant" not in text


def test_own_company_documents_use_their_own_banner():
    blocks = build_document("ropa", records_facts("self"), REGISTER)
    assert blocks[1].text == SELF_BANNER
