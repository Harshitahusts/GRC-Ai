"""The DPDPA readiness plan is worked out from real state, never stored or ticked by hand."""

import re

from helpers import ALL_YES, create, csrf, post

from grc_agent import plan
from grc_agent.web import db


def test_obligations_are_grouped_by_the_provision_they_come_from():
    assert plan.theme_of("Section 6(4)") == "consent"
    assert plan.theme_of("Section 8(6)") == "security"
    assert plan.theme_of("Section 8(7)") == "retention"
    assert plan.theme_of("Section 8(2)") == "processors"
    assert plan.theme_of("Section 8(10)") == "rights"
    assert plan.theme_of("Section 12(1)") == "rights"
    assert plan.theme_of("Section 9(1)") == "children"
    assert plan.theme_of("Section 16(1)") == "transfers"
    assert plan.theme_of("Section 8(9)") == "accountability"
    assert plan.theme_of("Rule 7") == "other"


def test_a_new_client_starts_at_scoping(authed):
    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/plan").text
    assert "Readiness plan" in page and "Start here" in page
    assert re.search(r'id="step-scope" class="card plan-step\s+is-current', page)
    assert "Intake submitted" in page and "Review and deliver" in page


def test_progress_follows_the_real_state(authed, app):
    eid = create(authed)
    base = f"/engagements/{eid}"
    post(authed, f"{base}/intake", {**ALL_YES, "q_Q-BREACH": "no", "action": "submit"})
    post(authed, f"{base}/assess")
    page = authed.get(f"{base}/plan").text
    # Children's data and transfers don't apply to this client, so they aren't steps.
    assert "Children&#39;s data" not in page and "Transfers outside India" not in page
    assert "Security and breaches" in page and "Notice and consent" in page
    # The breach gap's next step is a task, pre-filled for that obligation.
    assert "Create a task to close the gap" in page and "Close+gap%3A+Section+8%286%29" in page

    # Evidence, then the control: the obligation is done when the control is implemented.
    authed.post(
        f"{base}/evidence",
        data={"csrf": csrf(authed, "/"), "obligation_id": "OBL-004", "title": "ISMS policy"},
        files={
            "file": ("isms.txt", b"Access control, encryption and logging policy.", "text/plain")
        },
    )
    post(authed, f"{base}/controls/OBL-004", {"status": "implemented"})
    with db.connect(app.state.db_path) as conn:
        controls = conn.execute(
            "SELECT obligation_id, status FROM controls WHERE engagement_id = ?", (eid,)
        ).fetchall()
    assert ("OBL-004", "implemented") in [tuple(r) for r in controls]
    page = authed.get(f"{base}/plan").text
    section = page[page.index('id="step-security"') : page.index('id="step-rights"')]
    assert "1/2 done" in section
