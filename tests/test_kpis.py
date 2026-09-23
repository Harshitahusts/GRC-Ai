import json
from pathlib import Path

import pytest

from grc_agent.kpis import CorpusIndex, build_scorecard, load_engagements
from grc_agent.kpis.cli import main
from grc_agent.kpis.models import EngagementFormatError, parse_engagement

EXAMPLES = Path(__file__).parent.parent / "examples" / "kpis"
INDEX = CorpusIndex(["Section 5(1)", "Section 6(1)", "Rule 3"])


def engagement(**overrides):
    data = {
        "id": "ENG-1",
        "client": "Test Co",
        "mode": "agent",
        "completed": True,
        "consultant_hours": 5,
        "register_size": 2,
        "intake_completed_unaided": True,
        "intake_submitted_at": "2026-10-01T10:00:00+05:30",
        "draft_pack_ready_at": "2026-10-01T10:15:00+05:30",
        "findings": [
            {
                "id": "F-1",
                "obligation_id": "OBL-1",
                "status": "gap",
                "citations": ["Section 5(1)"],
                "verdict": "correct",
            },
            {
                "id": "F-2",
                "obligation_id": "OBL-2",
                "status": "compliant",
                "citations": ["Rule 3"],
                "verdict": "correct",
            },
        ],
        "documents": [
            {
                "type": "ropa",
                "outcome": "usable",
                "reviewed_by": "H",
                "reviewed_at": "2026-10-01T11:00:00+05:30",
            },
        ],
    }
    data.update(overrides)
    return parse_engagement(data)


def kpis(card):
    return {k.key: k for k in card.kpis}


def test_clean_engagement_is_verified_and_passes_everything():
    card = build_scorecard([engagement()], INDEX)
    assert card.verified_engagements == 1
    assert card.engagements[0].blockers == ()
    assert card.all_kpis_passing


@pytest.mark.parametrize(
    "overrides, blocker",
    [
        ({"completed": False}, "not completed"),
        ({"findings": []}, "no findings recorded"),
        ({"documents": []}, "no documents recorded"),
        ({"fell_back_to_manual": True}, "fell back to manual (marked as fallback)"),
        ({"documents": [{"type": "ropa"}]}, "1 document(s) not reviewed"),
        (
            {
                "documents": [
                    {
                        "type": "ropa",
                        "outcome": "full_rewrite",
                        "reviewed_by": "H",
                        "reviewed_at": "2026-10-01T11:00:00+05:30",
                    }
                ]
            },
            "fell back to manual (1 document(s) fully rewritten)",
        ),
        (
            {"findings": [{"id": "F", "obligation_id": "O", "status": "gap", "citations": []}]},
            "1 finding(s) without a citation",
        ),
        (
            {
                "findings": [
                    {"id": "F", "obligation_id": "O", "status": "gap", "citations": ["Section 77"]}
                ]
            },
            "1 unresolved citation(s)",
        ),
    ],
)
def test_north_star_blockers(overrides, blocker):
    card = build_scorecard([engagement(**overrides)], INDEX)
    assert card.verified_engagements == 0
    assert blocker in card.engagements[0].blockers


def test_fabricated_citation_fails_both_citation_kpis():
    bad = [
        {
            "id": "F",
            "obligation_id": "OBL-1",
            "status": "gap",
            "citations": ["Section 5(1)", "Section 77(2)"],
        }
    ]
    k = kpis(build_scorecard([engagement(findings=bad)], INDEX))
    assert k["fabricated_citations"].value == 1 and k["fabricated_citations"].passed is False
    assert "Section 77(2)" in k["fabricated_citations"].detail
    assert k["citation_resolution_rate"].value == 0.5


def test_obligation_coverage_reports_worst_engagement():
    partial = engagement(id="ENG-2", register_size=4)
    k = kpis(build_scorecard([engagement(), partial], INDEX))["obligation_coverage"]
    assert k.value == 0.5 and not k.passed and "ENG-2" in k.detail


def test_findings_correct_threshold_and_unscored():
    findings = [
        {
            "id": f"F-{i}",
            "obligation_id": f"O-{i}",
            "status": "gap",
            "citations": ["Rule 3"],
            "verdict": v,
        }
        for i, v in enumerate(["correct"] * 9 + ["wrong", None])
    ]
    k = kpis(build_scorecard([engagement(findings=findings)], INDEX))["findings_correct"]
    assert k.value == 0.9 and k.passed and "1 unscored" in k.detail


def test_hours_use_manual_baseline_and_only_completed_agent_engagements():
    manual = engagement(id="M", mode="manual", consultant_hours=20)
    unfinished = engagement(id="U", completed=False, consultant_hours=40)
    card = build_scorecard([manual, engagement(consultant_hours=7), unfinished], INDEX)
    k = kpis(card)["consultant_hours"]
    assert card.baseline_hours == 20
    assert k.value == 7 and not k.passed and "20.0 h" in k.detail
    assert card.agent_engagements == 2


def test_rates_and_timing():
    slow = engagement(
        id="S", intake_completed_unaided=False, draft_pack_ready_at="2026-10-01T10:45:00+05:30"
    )
    k = kpis(build_scorecard([engagement(), slow], INDEX))
    assert k["intake_unaided_rate"].value == 0.5 and not k["intake_unaided_rate"].passed
    assert k["minutes_to_draft_pack"].value == 30 and not k["minutes_to_draft_pack"].passed


def test_no_data_is_neither_pass_nor_fail():
    card = build_scorecard([], INDEX)
    assert all(k.passed is None for k in card.kpis)
    assert not card.all_kpis_passing
    assert card.warnings  # missing manual baseline


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"mode": "robot"}, "engagement.mode must be one of"),
        ({"consultant_hours": -1}, "consultant_hours must be a non-negative number"),
        (
            {"findings": [{"id": "F", "obligation_id": "O", "status": "meh", "citations": []}]},
            "findings[0].status",
        ),
        ({"draft_pack_ready_at": "2026-10-01T09:00:00+05:30"}, "before intake_submitted_at"),
        ({"draft_pack_ready_at": "2026-10-01T10:30:00"}, "both include a timezone"),
        ({"intake_submitted_at": "yesterday"}, "ISO 8601"),
    ],
)
def test_parse_errors_name_the_field(overrides, message):
    with pytest.raises(EngagementFormatError, match=message.replace("[", r"\[")):
        engagement(**overrides)


def test_load_rejects_duplicate_ids(tmp_path):
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(
            json.dumps({"id": "X", "client": "C", "mode": "manual", "completed": True})
        )
    with pytest.raises(EngagementFormatError, match="Duplicate"):
        load_engagements(tmp_path)


def test_cli_on_examples(capsys):
    index = str(EXAMPLES / "corpus_index.sample.txt")
    assert main([str(EXAMPLES / "engagements"), "--corpus-index", index]) == 0
    out = capsys.readouterr().out
    assert "Verified Engagements Delivered: 1 of 2" in out
    assert "Section 16(3)" in out

    assert main([str(EXAMPLES / "engagements"), "--corpus-index", index, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verified_engagements"] == 1


def test_cli_reports_bad_input(tmp_path, capsys):
    (tmp_path / "bad.json").write_text("{not json")
    index = tmp_path / "index.txt"
    index.write_text("Section 1\n")
    assert main([str(tmp_path), "--corpus-index", str(index)]) == 2
    assert "bad.json" in capsys.readouterr().err
