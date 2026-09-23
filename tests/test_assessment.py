import pytest

from grc_agent.assessment import assess, readiness_score
from grc_agent.kpis.citations import CorpusIndex
from grc_agent.register import corpus_index_path, load_register, parse_register

REGISTER = load_register()
INDEX = CorpusIndex.from_file(corpus_index_path())


def by_id(findings):
    return {f.obligation_id: f for f in findings}


def test_every_obligation_gets_exactly_one_finding():
    findings = assess(REGISTER, {}, INDEX)
    assert [f.obligation_id for f in findings] == [o.id for o in REGISTER.obligations]


def test_sample_register_citations_all_resolve():
    assert all(f.citation_resolves for f in assess(REGISTER, {}, INDEX))


def test_answers_map_to_statuses():
    f = by_id(
        assess(REGISTER, {"Q-NOTICE": "yes", "Q-CONSENT": "no", "Q-WITHDRAW": "not_sure"}, INDEX)
    )
    assert f["OBL-001"].status == "compliant" and not f["OBL-001"].remediation
    assert f["OBL-002"].status == "gap" and f["OBL-002"].remediation
    assert f["OBL-003"].status == "open_item"
    assert f["OBL-004"].status == "open_item"  # skipped is never a pass


def test_conditional_obligations():
    f = by_id(assess(REGISTER, {"CTX-CHILDREN": "no", "CTX-VENDORS": "not_sure"}, INDEX))
    assert f["OBL-011"].status == "not_applicable"
    assert f["OBL-012"].status == "open_item"  # can't tell whether it applies
    assert f["OBL-013"].status == "open_item"  # trigger skipped

    f = by_id(assess(REGISTER, {"CTX-CHILDREN": "yes", "Q-PARENTAL": "no"}, INDEX))
    assert f["OBL-011"].status == "gap" and f["OBL-011"].severity == "critical"


def test_unresolved_citation_is_flagged():
    findings = assess(REGISTER, {}, CorpusIndex(["Section 5(1)"]))
    assert by_id(findings)["OBL-001"].citation_resolves
    assert not by_id(findings)["OBL-002"].citation_resolves


def test_readiness_score_is_severity_weighted_and_ignores_not_applicable():
    rows = [
        {"status": "compliant", "severity": "critical"},  # 4
        {"status": "gap", "severity": "medium"},  # 2
        {"status": "open_item", "severity": "medium"},  # 2
        {"status": "not_applicable", "severity": "critical"},
    ]
    assert readiness_score(rows) == 50
    assert readiness_score([{"status": "not_applicable", "severity": "low"}]) is None


def test_same_answers_same_findings():
    answers = {"Q-NOTICE": "no", "CTX-FOREIGN": "yes", "Q-TRANSFER": "yes"}
    assert assess(REGISTER, answers, INDEX) == assess(REGISTER, answers, INDEX)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"severity": "urgent"}, "severity"),
        ({"question": "INFO-DATA"}, "not a choice question"),
        ({"applies_if": {"question": "NOPE", "equals": "yes"}}, "applies_if"),
    ],
)
def test_register_validation(change, message):
    data = {
        "version": "t",
        "questions": [
            {"id": "Q", "type": "choice", "section": "S", "text": "?"},
            {"id": "INFO-DATA", "type": "text", "section": "S", "text": "?"},
        ],
        "obligations": [
            {
                "id": "O",
                "question": "Q",
                "source": "Section 1",
                "severity": "low",
                "obligation": "o",
                "evidence": "e",
                "remediation": "r",
                **change,
            }
        ],
    }
    with pytest.raises(ValueError, match=message):
        parse_register(data)
