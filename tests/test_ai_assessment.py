import json
import re
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from grc_agent.ai_assessment import AssessmentError, ClaudeAssessor, gather_provisions
from grc_agent.config import Settings
from grc_agent.corpus import ingest, load_corpus
from grc_agent.register import parse_register

FIXTURE = Path(__file__).parent / "fixtures" / "corpus"

REGISTER = parse_register(
    {
        "version": "test",
        "questions": [
            {"id": "CTX-CHILDREN", "type": "choice", "section": "S", "text": "Any users under 18?"},
            {"id": "Q-NOTICE", "type": "choice", "section": "S", "text": "Do you show a notice?"},
            {
                "id": "Q-BREACH",
                "type": "choice",
                "section": "S",
                "text": "Do you have a breach plan?",
            },
            {"id": "Q-PARENT", "type": "choice", "section": "S", "text": "Parental consent?"},
        ],
        "obligations": [
            {
                "id": "OBL-1",
                "question": "Q-NOTICE",
                "source": "Section 3(1)",
                "severity": "high",
                "obligation": "Give a notice with every request for consent.",
                "evidence": "Forms",
                "remediation": "Add a notice.",
            },
            {
                "id": "OBL-2",
                "question": "Q-BREACH",
                "source": "Section 5(2)",
                "severity": "critical",
                "obligation": "Intimate a personal data breach to the Board and affected persons.",
                "evidence": "Plan",
                "remediation": "Write a plan.",
            },
            {
                "id": "OBL-3",
                "question": "Q-PARENT",
                "source": "Section 6(1)",
                "severity": "critical",
                "applies_if": {"question": "CTX-CHILDREN", "equals": "yes"},
                "obligation": "Obtain verifiable parental consent for a child's data.",
                "evidence": "Flow",
                "remediation": "Add consent flow.",
            },
        ],
    }
)
ANSWERS = {"Q-NOTICE": "yes", "Q-BREACH": "no", "CTX-CHILDREN": "no"}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    corpus_dir = tmp_path_factory.mktemp("c") / "corpus"
    shutil.copytree(FIXTURE, corpus_dir)
    ingest(corpus_dir)
    return load_corpus(corpus_dir / "build")


def reply(data, stop_reason="end_turn"):
    text = data if isinstance(data, str) else json.dumps(data)
    return SimpleNamespace(
        stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)]
    )


def good(citations, **extra):
    return reply(
        {
            "finding": "The client has no written breach plan.",
            "citations": citations,
            "remediation": "Adopt a breach playbook.",
            "confidence": "high",
            "needs_legal_review": False,
            **extra,
        }
    )


class FakeClient:
    """Replies with `script(prompt, attempt)`; records every request."""

    def __init__(self, script):
        self.script, self.calls, self.lock = script, [], threading.Lock()
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        with self.lock:
            self.calls.append(kwargs)
            attempt = sum(c["messages"][0]["content"] == prompt for c in self.calls)
        return self.script(prompt, attempt)


def cite_first_provision(prompt, attempt):
    return good([re.search(r"^\[([^\]]+)\]", prompt, re.M)[1]])


def assessor(corpus, script):
    client = FakeClient(script)
    return ClaudeAssessor(corpus, client=client, settings=Settings(model="m", effort="low")), client


def by_id(findings):
    return {f.obligation_id: f for f in findings}


def test_claude_drafts_applicable_findings_and_rules_keep_status(corpus):
    a, client = assessor(corpus, cite_first_provision)
    f = by_id(a.assess(REGISTER, ANSWERS))
    assert len(client.calls) == 2  # OBL-3 is not applicable: no model call
    assert f["OBL-3"].status == "not_applicable" and f["OBL-3"].drafted_by == "rules"
    assert f["OBL-2"].status == "gap" and f["OBL-2"].severity == "critical"
    assert f["OBL-2"].drafted_by == "claude" and f["OBL-2"].confidence == "high"
    assert f["OBL-2"].citations == ("Section 5(2)",) and f["OBL-2"].citation_resolves
    assert f["OBL-1"].status == "compliant" and f["OBL-1"].remediation == ""
    assert "Section 5(2)" in f["OBL-2"].provisions


def test_request_shape(corpus):
    a, client = assessor(corpus, cite_first_provision)
    a.assess(REGISTER, ANSWERS)
    call = client.calls[0]
    assert call["model"] == "m" and call["fallbacks"] == "default"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["thinking"] == {"type": "adaptive"}


def test_only_relevant_answers_are_sent(corpus):
    a, client = assessor(corpus, cite_first_provision)
    a.assess(REGISTER, ANSWERS)
    breach_prompt = next(
        c["messages"][0]["content"] for c in client.calls if "OBL-2" in c["messages"][0]["content"]
    )
    assert "breach plan" in breach_prompt
    assert "show a notice" not in breach_prompt and "under 18" not in breach_prompt


def test_governing_provision_always_included(corpus):
    provisions = [c.ref for c in gather_provisions(corpus, REGISTER.obligations[1])]
    assert provisions[0] == "Section 5(2)" and len(provisions) <= 5


@pytest.mark.parametrize(
    "citation, grounded",
    [
        ("Section 5(2)", True),
        ("Sec. 5 (2)", True),  # formatting is normalized
        ("Section 5", True),  # parent of a provision shown
        ("Section 7", False),  # real provision, but Claude wasn't shown it
        ("Section 77(3)", False),  # fabricated
        ("DPDP Act s.8", False),  # unparseable
    ],
)
def test_citation_checks(corpus, citation, grounded):
    a, _ = assessor(corpus, lambda p, n: good([citation]))
    f = by_id(a.assess(REGISTER, ANSWERS))["OBL-2"]
    assert f.citation_resolves is grounded
    assert (citation in f.unresolved) is not grounded


def test_malformed_output_is_retried_once(corpus):
    def script(prompt, attempt):
        return reply("not json {") if attempt == 1 else cite_first_provision(prompt, attempt)

    a, client = assessor(corpus, script)
    assert by_id(a.assess(REGISTER, ANSWERS))["OBL-2"].drafted_by == "claude"
    assert len(client.calls) == 4


@pytest.mark.parametrize(
    "bad",
    [
        reply("not json"),
        reply({"finding": "x"}),  # missing fields
        good([]),  # no citations
        reply({"finding": "cut", "citations": ["Section 5(2)"]}, stop_reason="max_tokens"),
    ],
)
def test_second_failure_is_a_hard_fail(corpus, bad):
    a, _ = assessor(corpus, lambda p, n: bad)
    with pytest.raises(AssessmentError, match="after a retry"):
        a.assess(REGISTER, ANSWERS)


def test_refusal_fails_the_run(corpus):
    a, _ = assessor(corpus, lambda p, n: reply("", stop_reason="refusal"))
    with pytest.raises(AssessmentError, match="declined"):
        a.assess(REGISTER, ANSWERS)


def test_missing_provision_fails_the_run(corpus):
    register = parse_register(
        {
            "version": "t",
            "questions": [{"id": "Q", "type": "choice", "section": "S", "text": "zzz"}],
            "obligations": [
                {
                    "id": "O",
                    "question": "Q",
                    "source": "Section 99",
                    "severity": "low",
                    "obligation": "qqq xyzzy",
                    "evidence": "e",
                    "remediation": "r",
                }
            ],
        }
    )
    a, _ = assessor(corpus, cite_first_provision)
    with pytest.raises(AssessmentError, match="no provisions found"):
        a.assess(register, {"Q": "no"})
