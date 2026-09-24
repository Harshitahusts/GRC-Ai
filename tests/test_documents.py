import io

from docx import Document

from grc_agent.assessment import assess
from grc_agent.documents import (
    DOCUMENT_TYPES,
    DPA_BANNER,
    DRAFT_BANNER,
    EngagementFacts,
    build_document,
    to_docx,
)
from grc_agent.kpis.citations import CorpusIndex
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
