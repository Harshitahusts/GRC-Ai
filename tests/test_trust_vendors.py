"""The public DPDP trust page, and vendor questionnaires answered by link."""

import json
import re

from helpers import MEMBER_PASSWORD, add_member, create, csrf, post

from grc_agent import vendor_questions as vq
from grc_agent.web import db as webdb


def db(app):
    return webdb.connect(app.state.db_path)


def rid_of(r) -> int:
    return int(r.url.path.rsplit("/", 1)[1])


def all_yes() -> dict:
    return {f"a_{q.id}": "yes" for q in vq.QUESTIONS}


# ---------------------------------------------------------------- scoring


def test_score_and_critical_answers():
    yes = {q.id: "yes" for q in vq.QUESTIONS}
    assert vq.score(yes, []) == {"pct": 100, "risk": "low", "concerns": [], "critical": []}
    # One essential "no" makes a vendor high risk whatever the total.
    r = vq.score({**yes, "breach": "no"}, ["iso27001"])
    assert r["risk"] == "high" and r["pct"] > 80 and len(r["critical"]) == 1
    # Partly on several good-practice questions: medium.
    some = {
        **yes,
        **{
            k: "partly"
            for k in (
                "logging",
                "backups",
                "testing",
                "training",
                "breach_plan",
                "audit",
                "rights",
                "access",
            )
        },
    }
    assert vq.score(some, [])["risk"] == "medium"
    # Not applicable doesn't count against them.
    assert vq.score({**yes, "children": "na"}, [])["pct"] == 100
    # Unanswered counts as no.
    assert vq.score({}, [])["risk"] == "high"


# ---------------------------------------------------------------- vendor questionnaire


def new_vendor(authed, eid):
    r = post(
        authed,
        f"/engagements/{eid}/r/vendors",
        {"name": "CloudCRM", "service": "CRM", "contract": "negotiating"},
    )
    return rid_of(r)


def send(authed, eid, rid, email="") -> str:
    page = post(
        authed,
        f"/engagements/{eid}/r/vendors/{rid}/questionnaire",
        {"email": email},
        follow_redirects=True,
    ).text
    return re.search(r'value="https?://[^"]+(/vq/[^"]+)"', page).group(1)


def test_questionnaire_round_trip(app, authed, client):
    eid = create(authed)
    rid = new_vendor(authed, eid)
    path = send(authed, eid, rid)
    from fastapi.testclient import TestClient

    outside = TestClient(app)
    form = outside.get(path)
    assert form.status_code == 200 and "CloudCRM" in form.text
    assert form.headers["cache-control"] == "no-store"

    # Missing answers are refused and the form keeps what was typed.
    r = outside.post(path, data={"a_contract": "yes", "respondent": "Priya"})
    assert r.status_code == 400 and "Answer every question" in r.text and "Priya" in r.text

    data = {
        **all_yes(),
        "a_breach": "no",
        "location": "both",
        "countries": "USA",
        "respondent": "Priya",
        "role": "CISO",
        "certifications": ["soc2"],
        "c_encryption": "AES-256 at rest",
    }
    r = outside.post(path, data=data, follow_redirects=True)
    assert "Thank you" in r.text
    # It can't be submitted twice.
    assert "Thank you" in outside.post(path, data=data, follow_redirects=True).text

    with db(app) as conn:
        q = conn.execute("SELECT * FROM vendor_questionnaires").fetchone()
        rec = conn.execute("SELECT data_json FROM records WHERE id = ?", (rid,)).fetchone()
    assert q["status"] == "submitted" and q["risk"] == "high" and q["respondent"] == "Priya"
    d = json.loads(rec["data_json"])
    assert d["questionnaire"] == "received" and d["risk"] == "high"
    assert d["location"] == "both" and d["countries"] == "USA"

    detail = authed.get(f"/engagements/{eid}/r/vendors/{rid}").text
    assert "Questionnaire answered" in detail and "To review" in detail
    view = authed.get(f"/engagements/{eid}/vendor-questionnaires/{q['id']}").text
    assert "AES-256 at rest" in view and "SOC 2 Type II" in view and "Concerns" in view

    r = post(
        authed,
        f"/engagements/{eid}/vendor-questionnaires/{q['id']}/review",
        {"risk": "medium", "note": "DPA adds 24h breach clause"},
        follow_redirects=True,
    )
    assert "Questionnaire marked reviewed" in r.text
    with db(app) as conn:
        d = json.loads(
            conn.execute("SELECT data_json FROM records WHERE id = ?", (rid,)).fetchone()[0]
        )
    assert d["questionnaire"] == "reviewed" and d["risk"] == "medium"


def test_new_link_cancels_old_and_bad_links_404(app, authed):
    from fastapi.testclient import TestClient

    eid = create(authed)
    rid = new_vendor(authed, eid)
    old = send(authed, eid, rid)
    new = send(authed, eid, rid)
    outside = TestClient(app)
    assert outside.get(old).status_code == 404
    assert outside.get(new).status_code == 200
    assert outside.get("/vq/not-a-real-token").status_code == 404
    with db(app) as conn:
        conn.execute("UPDATE vendor_questionnaires SET expires_at = '2020-01-01T00:00:00+00:00'")
    assert outside.get(new).status_code == 404


def test_questionnaire_is_fenced(app, authed):
    from fastapi.testclient import TestClient

    eid = create(authed)
    rid = new_vendor(authed, eid)
    add_member(app, "stranger")
    other = TestClient(app)
    other.post(
        "/login", data={"username": "stranger", "password": MEMBER_PASSWORD, "csrf": csrf(other)}
    )
    assert post(other, f"/engagements/{eid}/r/vendors/{rid}/questionnaire", {}).status_code == 404
    # A vendor id from another engagement isn't accepted either.
    eid2 = create(authed)
    assert post(authed, f"/engagements/{eid2}/r/vendors/{rid}/questionnaire", {}).status_code == 404


# ---------------------------------------------------------------- trust page


def test_trust_page(app, authed):
    from fastapi.testclient import TestClient

    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/trust")
    assert page.status_code == 200 and 'value="acme-pvt-ltd"' in page.text

    # Records it draws on: a published policy, a system, an active vendor (and one not).
    r = post(
        authed,
        f"/engagements/{eid}/r/policies",
        {
            "title": "Privacy notice",
            "kind": "notice",
            "version": "3",
            "link": "https://acme.in/privacy",
            "approved_by": "CEO",
            "approved_on": "2026-02-01",
        },
    )
    post(authed, f"/engagements/{eid}/r/policies/{rid_of(r)}/status", {"status": "published"})
    post(
        authed,
        f"/engagements/{eid}/r/systems",
        {
            "name": "Billing DB",
            "encryption": "yes",
            "access_control": "yes",
            "logging": "no",
            "backups": "yes",
            "purpose": "SECRET internal note",
        },
    )
    r = post(
        authed,
        f"/engagements/{eid}/r/vendors",
        {"name": "Razorpay", "service": "Payments", "location": "india", "contract": "signed"},
    )
    post(authed, f"/engagements/{eid}/r/vendors/{rid_of(r)}/status", {"status": "active"})
    post(authed, f"/engagements/{eid}/r/vendors", {"name": "HiddenVendor", "contract": "none"})

    settings = {
        "slug": "acme",
        "company": "Acme",
        "show_policies": "1",
        "show_safeguards": "1",
        "show_subprocessors": "1",
        "show_programme": "1",
    }
    # Publishing needs a contact (Section 8(9)).
    r = post(
        authed, f"/engagements/{eid}/trust", {**settings, "published": "1"}, follow_redirects=True
    )
    assert "Add a contact email" in r.text
    outside = TestClient(app)
    post(authed, f"/engagements/{eid}/trust", {**settings, "contact_email": "dpo@acme.in"})
    assert outside.get("/trust/acme").status_code == 404  # saved, not published
    assert "Preview" in authed.get(f"/engagements/{eid}/trust/preview").text

    post(
        authed,
        f"/engagements/{eid}/trust",
        {**settings, "contact_email": "dpo@acme.in", "published": "1"},
    )
    pub = outside.get("/trust/acme")
    assert pub.status_code == 200
    t = pub.text
    assert "Privacy notice" in t and "https://acme.in/privacy" in t and "v3" in t
    assert "Razorpay" in t and "HiddenVendor" not in t
    assert "Personal data encrypted or masked" in t and "dpo@acme.in" in t
    assert "SECRET internal note" not in t and "Billing DB" not in t
    assert "Notice and consent" in t

    # Address rules.
    for bad in ("A!", "ab", "admin"):
        r = post(
            authed, f"/engagements/{eid}/trust", {**settings, "slug": bad}, follow_redirects=True
        )
        assert "Web address" in r.text
    eid2 = create(authed)
    r = post(
        authed, f"/engagements/{eid2}/trust", {**settings, "slug": "acme"}, follow_redirects=True
    )
    assert "taken" in r.text
