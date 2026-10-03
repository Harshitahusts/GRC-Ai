"""AI drafts, people decide: AI-written findings need a person's review before delivery."""

import re

from helpers import ALL_YES, create, post

from grc_agent.web import db


def _ready_engagement(authed, app, drafted_by="groq"):
    """An assessed engagement whose findings look AI-drafted, documents reviewed."""
    eid = create(authed)
    base = f"/engagements/{eid}"
    post(authed, f"{base}/intake", {**ALL_YES, "action": "submit"})
    post(authed, f"{base}/assess")
    with db.connect(app.state.db_path) as conn:
        conn.execute(
            "UPDATE findings SET drafted_by = ?, confidence = 'high' WHERE engagement_id = ?",
            (drafted_by, eid),
        )
    return eid, base


def _review_documents(authed, base):
    post(authed, f"{base}/documents/generate")
    page = authed.get(f"{base}/documents").text
    for did in sorted(set(re.findall(rf"{base}/documents/(\d+)", page))):
        post(authed, f"{base}/documents/{did}/review", {"outcome": "usable", "confirm": "on"})


def _finding_ids(authed, base):
    return re.findall(r'id="f(\d+)"', authed.get(f"{base}/findings").text)


def test_unreviewed_ai_findings_block_delivery(authed, app):
    eid, base = _ready_engagement(authed, app)
    page = authed.get(f"{base}/findings").text
    assert "Human review required" in page and "AI draft" in page
    _review_documents(authed, base)

    page = post(authed, f"{base}/deliver").text
    assert "Every AI-drafted finding reviewed by a person" in page

    for fid in _finding_ids(authed, base):
        post(authed, f"{base}/findings/{fid}", {"verdict": "correct"})
    page = authed.get(f"{base}/findings").text
    assert "Human review required" not in page and "Reviewed by harshit" in page
    post(authed, f"{base}/deliver")
    assert authed.get(f"/engagements/{eid}/export.json").json()["completed"]


def test_a_wrong_draft_must_be_rewritten_by_a_person(authed, app):
    eid, base = _ready_engagement(authed, app)
    fids = _finding_ids(authed, base)
    for fid in fids:
        post(authed, f"{base}/findings/{fid}", {"verdict": "correct"})
    post(authed, f"{base}/findings/{fids[0]}", {"verdict": "wrong"})
    assert "Rewrite required" in authed.get(f"{base}/findings").text
    _review_documents(authed, base)
    assert "Every finding marked wrong has been rewritten" in post(authed, f"{base}/deliver").text

    # Rewriting clears the draft pack (it quotes the findings), so review it again.
    page = post(
        authed,
        f"{base}/findings/{fids[0]}/edit",
        {"summary": "Rewritten by the consultant.", "remediation": "Do the thing."},
    ).text
    assert "Documents were cleared" in page and "Rewritten by harshit" in page
    assert "Rewritten by the consultant." in page
    _review_documents(authed, base)
    post(authed, f"{base}/deliver")
    assert authed.get(f"/engagements/{eid}/export.json").json()["completed"]

    with db.connect(app.state.db_path) as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_log")]
    assert "finding_edited" in actions


def test_rules_findings_need_no_review(authed, app):
    eid, base = _ready_engagement(authed, app, drafted_by="rules")
    assert "Human review required" not in authed.get(f"{base}/findings").text
    _review_documents(authed, base)
    post(authed, f"{base}/deliver")
    assert authed.get(f"/engagements/{eid}/export.json").json()["completed"]


def test_an_empty_rewrite_is_refused(authed, app):
    _, base = _ready_engagement(authed, app)
    fid = _finding_ids(authed, base)[0]
    page = post(authed, f"{base}/findings/{fid}/edit", {"summary": "  "}).text
    assert "can&#39;t be empty" in page
