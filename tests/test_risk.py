"""DPDPA risk register: built from findings and evidence, scored L x I, editable."""

from datetime import date

import pytest
from helpers import ALL_YES, create, post

from grc_agent import risk
from grc_agent.register import load_register

OBLIGATIONS = {o.id: o for o in load_register().obligations}


def finding(obl, status, severity="high", fid=1):
    return {
        "id": fid,
        "obligation_id": obl,
        "status": status,
        "severity": severity,
        "summary": "Client says this isn't in place.",
        "remediation": "Fix it.",
        "citation": OBLIGATIONS[obl].source,
    }


EVIDENCE = [
    {
        "connector": "aws",
        "check_key": "root_mfa",
        "status": "fail",
        "title": "MFA on the root account",
        "detail": "No MFA.",
        "provisions": ["Section 8(5)"],
    },
    {
        "connector": "aws",
        "check_key": "cloudtrail",
        "status": "pass",
        "title": "Audit logging",
        "detail": "On.",
        "provisions": ["Section 8(5)"],
    },
]


def test_findings_and_failed_checks_become_scored_risks():
    risks = risk.build(
        1,
        [
            finding("OBL-004", "gap", "critical", 1),  # security: 4 x 5
            finding("OBL-013", "open_item", "high", 2),  # transfer: 3 x 4
            finding("OBL-001", "compliant", "high", 3),  # no risk
        ],
        OBLIGATIONS,
        EVIDENCE,
        {},
    )
    by_key = {r.key: r for r in risks}
    assert set(by_key) == {"finding:OBL-004", "finding:OBL-013", "evidence:aws:root_mfa"}
    sec = by_key["finding:OBL-004"]
    assert (sec.likelihood, sec.impact, sec.score, sec.level) == (4, 5, 20, "critical")
    assert sec.threat == "A personal data breach through weak safeguards"
    assert sec.action == "Fix it." and sec.treatment == "mitigate"
    transfer = by_key["finding:OBL-013"]
    assert transfer.score == 12 and transfer.level == "high"
    assert transfer.threat == "Personal data transferred to a restricted country"
    assert transfer.vulnerability.startswith("Not confirmed yet")
    mfa = by_key["evidence:aws:root_mfa"]
    assert (mfa.likelihood, mfa.impact, mfa.source) == (4, 4, "evidence")
    assert [r.key for r in risks][0] == "finding:OBL-004"  # highest first


def test_edits_survive_and_closed_risks_drop_out():
    edits = {
        "finding:OBL-004": {"likelihood": 2, "impact": 5, "owner": "CTO", "status": "closed"},
        "finding:OBL-013": {"treatment": "accept", "notes": "Board approved", "due": "2000-01-01"},
    }
    risks = risk.build(
        1,
        [finding("OBL-004", "gap", "critical", 1), finding("OBL-013", "gap", "high", 2)],
        OBLIGATIONS,
        [],
        edits,
    )
    sec, transfer = (
        next(r for r in risks if "004" in r.key),
        next(r for r in risks if "013" in r.key),
    )
    assert sec.edited and sec.score == 10 and sec.owner == "CTO"
    assert risks[-1] is sec  # closed risks sort last
    assert transfer.treatment == "accept" and transfer.overdue(date(2026, 1, 1))
    assert not sec.overdue()
    s = risk.summary(risks)
    assert s == {
        "open": 1,
        "closed": 1,
        "by_level": {"critical": 0, "high": 1, "medium": 0, "low": 0},
        "overdue": 1,
        "unowned": 1,
        "accepted": 1,
    }
    cells = [c for row in risk.heatmap(risks) for c in row if c["count"]]
    assert [(c["likelihood"], c["impact"], c["count"]) for c in cells] == [(4, 4, 1)]


def test_heatmap_layout():
    grid = risk.heatmap([])
    assert len(grid) == 5 and [c["impact"] for c in grid[0]] == [5] * 5
    assert grid[0][4]["level"] == "critical" and grid[4][0]["level"] == "low"


# ---- web


@pytest.fixture
def assessed(authed):
    eid = create(authed)
    answers = {**ALL_YES, "q_Q-SECURITY": "no", "q_Q-BREACH": "not_sure"}
    post(authed, f"/engagements/{eid}/intake", {**answers, "action": "submit"})
    post(authed, f"/engagements/{eid}/assess", {"mode": "rules"})
    return authed, eid


def test_risk_page(assessed):
    client, eid = assessed
    page = client.get(f"/engagements/{eid}/risks").text
    assert "Risk matrix" in page and "Risk register" in page
    assert "A personal data breach through weak safeguards" in page
    assert 'id="finding:OBL-004"' in page


def test_editing_a_risk(assessed):
    client, eid = assessed
    base = f"/engagements/{eid}/risks"
    edit = {"risk_key": "finding:OBL-004", "likelihood": "2", "impact": "5", "owner": "CTO"}
    page = post(client, base, {**edit, "treatment": "mitigate", "status": "in_progress"}).text
    assert "Saved:" in page and "CTO" in page and "In progress" in page
    # Accepting needs a reason.
    page = post(client, base, {**edit, "treatment": "accept", "status": "open"}).text
    assert "Accepting a risk needs a reason" in page
    page = post(
        client, base, {**edit, "treatment": "accept", "status": "open", "notes": "Board OK"}
    ).text
    assert "Note: Board OK" in page
    assert "risk accepted" in client.get("/notifications").text
    bad = post(client, base, {**edit, "risk_key": "finding:NOPE", "treatment": "mitigate"})
    assert bad.status_code == 404
    bad = post(client, base, {**edit, "treatment": "ignore", "status": "open"})
    assert bad.status_code == 400
    page = post(client, base, {**edit, "treatment": "mitigate", "due": "next week"}).text
    assert "Use a date for the due date" in page


def test_overview_and_dashboard_show_risks(assessed):
    client, eid = assessed
    overview = client.get(f"/engagements/{eid}").text
    assert "Risk &amp; data snapshot" in overview and "KPI details" not in overview
    dashboard = client.get("/").text
    assert "Top risks" in dashboard and "Critical &amp; high risks" in dashboard
    assert "Pilot KPIs" not in dashboard and "North Star" not in dashboard
