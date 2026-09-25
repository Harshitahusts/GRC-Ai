"""Client data-flow map: built from intake, findings, evidence and custom systems."""

import csv
import io

import pytest
from helpers import ALL_YES, create, post

from grc_agent import dataflow
from grc_agent.register import load_register

OBLIGATIONS = {o.id: o for o in load_register().obligations}
ANSWERS = {
    "INFO-DATA": "Names, emails and phone numbers",
    "INFO-TOOLS": "Zoho CRM, Mailchimp, Local accountant",
    "INFO-RETENTION": "Five years after the account closes",
    "CTX-FOREIGN": "yes",
    "CTX-CHILDREN": "no",
}


def finding(fid, obl, status, severity="high", remediation="Do the thing."):
    return {
        "id": fid,
        "obligation_id": obl,
        "status": status,
        "severity": severity,
        "summary": "Client says this isn't in place.",
        "remediation": remediation,
        "citation": OBLIGATIONS[obl].source,
    }


def build(answers=ANSWERS, findings=(), evidence=(), custom=(), assessed=True):
    return dataflow.build(
        1, answers, list(findings), OBLIGATIONS, list(evidence), list(custom), assessed
    )


def nodes(flow):
    return {n["id"]: n for n in flow["nodes"]}


def test_intake_answers_become_the_flow():
    flow = build()
    n = nodes(flow)
    assert {"customers", "collect", "rights", "core", "deletion", "abroad"} <= set(n)
    assert "children" not in n
    assert n["vendor-zoho-crm"]["location"] == "india"
    assert n["vendor-mailchimp"]["location"] == "outside"
    assert n["vendor-local-accountant"]["location"] == "unknown"
    assert flow["categories"] == ["Emails", "Names", "Phone numbers"]
    assert n["deletion"]["note"] == "Client says: Five years after the account closes"
    pairs = {(e["source"], e["target"]) for e in flow["edges"]}
    assert ("core", "vendor-mailchimp") in pairs and ("vendor-mailchimp", "abroad") in pairs
    assert ("vendor-zoho-crm", "abroad") not in pairs
    assert flow["summary"]["leaves_india"] is True


def test_children_and_no_foreign_services():
    flow = build({**ANSWERS, "CTX-CHILDREN": "yes", "CTX-FOREIGN": "no", "INFO-TOOLS": "Zoho"})
    n = nodes(flow)
    assert "children" in n and "abroad" not in n
    assert flow["summary"]["leaves_india"] is False


def test_findings_are_pinned_to_the_step_they_affect():
    flow = build(
        findings=[
            finding(1, "OBL-001", "gap", "high", "Publish a privacy notice."),
            finding(2, "OBL-013", "gap", "critical", "Map every transfer."),
            finding(3, "OBL-012", "open_item", "high", "Sign processor contracts."),
            finding(4, "OBL-004", "compliant"),
        ]
    )
    n = nodes(flow)
    assert [i["title"] for i in n["collect"]["issues"]] == [OBLIGATIONS["OBL-001"].obligation]
    assert n["collect"]["status"] == "serious"  # high-severity gap
    assert n["abroad"]["status"] == "critical"
    assert all(n[v]["status"] == "warning" for v in n if v.startswith("vendor-"))
    assert n["core"]["status"] == "ok"  # compliant findings add nothing
    # The flow into Outside India turns critical too.
    assert {e["status"] for e in flow["edges"] if e["target"] == "abroad"} == {"critical"}
    # Plan: most urgent first; one step for the vendor issue, listing every vendor.
    plan = flow["plan"]
    assert plan[0]["action"] == "Map every transfer."
    vendor_step = next(p for p in plan if p["action"] == "Sign processor contracts.")
    assert len(vendor_step["where"]) == 3
    assert flow["summary"]["issues"] == {"critical": 1, "serious": 1, "warning": 3}


def test_before_assessment_steps_are_pending():
    n = nodes(build(assessed=False))
    assert n["core"]["status"] == "pending" and n["customers"]["status"] == "ok"


def test_connector_evidence_adds_live_systems():
    evidence = [
        {
            "connector": "aws",
            "check_key": "data_location",
            "status": "warn",
            "title": "Where data is stored",
            "detail": "Buckets in us-east-1.",
            "provisions": ["Section 16(1)"],
            "data": {"outside_india": ["us-east-1"], "inside_india": ["ap-south-1"]},
        },
        {
            "connector": "aws",
            "check_key": "root_mfa",
            "status": "fail",
            "title": "MFA on the root account",
            "detail": "No MFA.",
            "provisions": ["Section 8(5)"],
            "data": {},
        },
    ]
    flow = build({**ANSWERS, "CTX-FOREIGN": "no", "INFO-TOOLS": ""}, evidence=evidence)
    n = nodes(flow)
    assert n["aws"]["location"] == "outside" and n["aws"]["status"] == "serious"
    assert "us-east-1" in n["abroad"]["note"]
    assert ("aws", "abroad") in {(e["source"], e["target"]) for e in flow["edges"]}
    assert any(i["title"] == "Where data is stored" for i in n["abroad"]["issues"])


def test_custom_systems_join_the_flow():
    custom = [
        {
            "id": 5,
            "name": "Payroll bureau",
            "stage": "vendors",
            "location": "outside",
            "categories": "salary, bank details",
            "source": "",
        }
    ]
    flow = build({**ANSWERS, "CTX-FOREIGN": "no", "INFO-TOOLS": ""}, custom=custom)
    n = nodes(flow)
    assert n["custom-5"]["categories"] == ["Salary", "Bank details"]
    pairs = {(e["source"], e["target"]) for e in flow["edges"]}
    assert ("core", "custom-5") in pairs and ("custom-5", "abroad") in pairs
    assert flow["summary"]["leaves_india"] is True


def test_version_changes_only_when_the_flow_does():
    assert build()["version"] == build()["version"]
    assert build()["version"] != build({**ANSWERS, "INFO-TOOLS": "Slack"})["version"]


# ---- web


@pytest.fixture
def assessed(authed):
    eid = create(authed)
    answers = {**ALL_YES, "q_INFO-TOOLS": "Mailchimp, Zoho", "q_CTX-FOREIGN": "yes"}
    answers["q_Q-TRANSFER"] = "no"
    answers["q_Q-VENDOR-CONTRACT"] = "no"
    post(authed, f"/engagements/{eid}/intake", {**answers, "action": "submit"})
    post(authed, f"/engagements/{eid}/assess", {"mode": "rules"})
    return authed, eid


def test_map_page_and_live_json(assessed):
    client, eid = assessed
    page = client.get(f"/engagements/{eid}/dataflow").text
    assert 'id="flow-svg"' in page and "Mitigation plan" in page and "/static/dataflow.js" in page
    assert "Leaves India?" in page
    data = client.get(f"/engagements/{eid}/dataflow.json").json()
    assert data["summary"]["leaves_india"] is True
    assert any(n["id"] == "vendor-mailchimp" for n in data["nodes"])
    abroad = next(n for n in data["nodes"] if n["id"] == "abroad")
    assert abroad["status"] in ("serious", "critical")  # the transfer gap
    assert "Data flows" in client.get("/dataflows").text


def test_map_updates_when_the_intake_changes(assessed):
    client, eid = assessed
    before = client.get(f"/engagements/{eid}/dataflow.json").json()["version"]
    answers = {**ALL_YES, "q_INFO-TOOLS": "Mailchimp, Zoho, Slack", "q_CTX-FOREIGN": "yes"}
    post(client, f"/engagements/{eid}/intake", {**answers, "action": "save"})
    after = client.get(f"/engagements/{eid}/dataflow.json").json()
    assert after["version"] != before
    assert any(n["id"] == "vendor-slack" for n in after["nodes"])


def test_add_and_remove_a_system(assessed):
    client, eid = assessed
    page = post(
        client,
        f"/engagements/{eid}/dataflow/nodes",
        {"name": "Payroll bureau", "stage": "vendors", "location": "india", "categories": "salary"},
    ).text
    assert "Added Payroll bureau" in page
    data = client.get(f"/engagements/{eid}/dataflow.json").json()
    node = next(n for n in data["nodes"] if n["name"] == "Payroll bureau")
    page = post(client, f"/engagements/{eid}/dataflow/nodes/{node['custom_id']}/delete").text
    assert "Removed Payroll bureau" in page
    bad = {"name": "", "stage": "nowhere", "location": "mars"}
    assert "Give the system a name" in post(client, f"/engagements/{eid}/dataflow/nodes", bad).text


def test_plan_csv_is_safe_for_spreadsheets(authed):
    eid = create(authed)
    answers = {**ALL_YES, "q_CTX-VENDORS": "no", "q_Q-VENDOR-CONTRACT": "no"}
    post(authed, f"/engagements/{eid}/intake", {**answers, "action": "submit"})
    post(authed, f"/engagements/{eid}/assess", {"mode": "rules"})
    post(
        authed,
        f"/engagements/{eid}/dataflow/nodes",
        {"name": "=HYPERLINK(1)", "stage": "vendors", "location": "india"},
    )
    response = authed.get(f"/engagements/{eid}/dataflow/plan.csv")
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][:4] == ["Step", "Level", "Where in the flow", "Issue"]
    assert not any(cell.startswith(("=", "+", "-", "@")) for row in rows for cell in row)


def test_safe_cell():
    from grc_agent.web.dataflow_views import safe_cell

    assert safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)" and safe_cell("-1+1") == "'-1+1"
    assert safe_cell("Mailchimp") == "Mailchimp" and safe_cell(3) == 3


def test_manual_engagements_have_no_map(authed):
    eid = create(authed, mode="manual")
    assert authed.get(f"/engagements/{eid}/dataflow").status_code == 400
