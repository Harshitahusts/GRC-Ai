"""The full obligations register, its trend and penalty exposure, the new registers
(erasure, accepted gaps, systems, drills), the Board inquiry pack and re-check reminders."""

import io
import zipfile
from datetime import date, timedelta

from helpers import ALL_YES, MEMBER_PASSWORD, add_member, create, csrf, post

from grc_agent import obligations as cat
from grc_agent.web import db as webdb
from grc_agent.web.registers import REGISTERS, status_problems


def db(app):
    return webdb.connect(app.state.db_path)


# ---------------------------------------------------------------- the catalogue


def test_catalogue_is_well_formed():
    ids = [i.id for i in cat.CATALOGUE]
    assert len(ids) == len(set(ids))
    for i in cat.CATALOGUE:
        assert i.penalty in cat.PENALTIES and i.scope in cat.SCOPES
        assert i.register == "" or i.register in REGISTERS, i.id
        assert i.duty and i.proof and i.ref
    # The Schedule's caps, in rupees.
    assert cat.PENALTIES["security"][1] == 250_00_00_000
    assert cat.crore(250_00_00_000) == "₹250 crore"
    assert cat.BY_ID["R-04"].applies_from == cat.NOV_2026


def test_scope_from_intake_answers():
    assert cat.scope_applies("all", {}) is True
    assert cat.scope_applies("children", {"CTX-CHILDREN": "no"}) is False
    assert cat.scope_applies("children", {"CTX-CHILDREN": "yes"}) is True
    assert cat.scope_applies("children", {}) is None
    assert cat.scope_applies("sdf", {"CTX-CHILDREN": "yes"}) is None


def test_exposure_counts_each_penalty_row_once():
    items = [
        {"item": cat.BY_ID["A-08-5"], "status": "partial"},
        {"item": cat.BY_ID["R-08-logs"], "status": "not_assessed"},
        {"item": cat.BY_ID["A-08-6"], "status": "compliant"},
        {"item": cat.BY_ID["A-04"], "status": "not_applicable"},
    ]
    ex = cat.exposure(items)
    assert [r["amount"] for r in ex["rows"]] == [250_00_00_000]
    assert len(ex["rows"][0]["duties"]) == 2 and ex["total_label"] == "₹250 crore"


# ---------------------------------------------------------------- the page


def test_obligations_page_suggests_and_saves(app, authed):
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", ALL_YES)
    page = authed.get(f"/engagements/{eid}/obligations")
    assert page.status_code == 200
    assert "Obligations register" in page.text and "Penalty exposure" in page.text
    assert "Section 8(6), Rule 7" in page.text and "₹250 crore" in page.text
    # No children (intake said no): suggested as not applying.
    assert 'id="A-09-1"' in page.text
    with db(app) as conn:
        assert conn.execute(
            "SELECT pct FROM readiness_history WHERE engagement_id = ?", (eid,)
        ).fetchone()

    base = f"/engagements/{eid}/obligations"
    # Compliant needs evidence or a note.
    r = post(authed, f"{base}/A-04", {"status": "compliant"}, follow_redirects=True)
    assert "attach evidence or describe" in r.text
    # Not applicable needs a reason.
    r = post(authed, f"{base}/A-04", {"status": "not_applicable"}, follow_redirects=True)
    assert "Say why" in r.text
    r = post(
        authed,
        f"{base}/A-04",
        {"status": "compliant", "note": "Purpose register kept", "review_date": "2026-01-01"},
        follow_redirects=True,
    )
    assert "Section 4: Compliant." in r.text
    with db(app) as conn:
        row = conn.execute(
            "SELECT * FROM obligation_reviews WHERE engagement_id = ? AND item_id = 'A-04'",
            (eid,),
        ).fetchone()
    assert row["status"] == "compliant" and row["updated_by"] == "harshit"
    # The past re-check date shows up as due, here and on the work queue.
    due = authed.get(f"{base}?show=recheck").text
    assert 'id="A-04"' in due and 'id="A-05"' not in due
    work = authed.get("/work").text
    assert "Re-checks due" in work and "Section 4: Lawful purpose" in work

    assert post(authed, f"{base}/NOPE", {"status": "compliant"}).status_code == 404
    assert post(authed, f"{base}/A-04", {"status": "great"}).status_code == 400
    assert authed.get(f"{base}?show=compliant").status_code == 200


def test_evidence_can_link_to_a_catalogue_duty(app, authed):
    eid = create(authed)
    r = authed.post(
        f"/engagements/{eid}/evidence",
        data={
            "csrf": csrf(authed, "/"),
            "title": "Retention schedule",
            "obligation_id": "R-08-48h",
        },
        files={
            "file": ("schedule.txt", b"Erase after 3 years with 48 hours notice.", "text/plain")
        },
    )
    assert r.status_code == 200
    with db(app) as conn:
        f = conn.execute("SELECT obligation_id FROM evidence_files").fetchone()
    assert f["obligation_id"] == "R-08-48h"
    page = authed.get(f"/engagements/{eid}/obligations").text
    assert "1 evidence file<" in page


def test_viewer_cannot_update_obligations(app, client):
    org = add_member(app, "olga", kind="client", team_role="admin")
    add_member(app, "vic", org=org, team_role="viewer")
    client.post(
        "/login",
        data={"username": "olga", "password": MEMBER_PASSWORD, "csrf": csrf(client)},
    )
    eid = create(client)
    client.cookies.clear()
    client.post(
        "/login",
        data={"username": "vic", "password": MEMBER_PASSWORD, "csrf": csrf(client)},
    )
    assert client.get(f"/engagements/{eid}/obligations").status_code == 200
    r = post(client, f"/engagements/{eid}/obligations/A-04", {"status": "partial", "note": "x"})
    assert r.status_code == 403


# ---------------------------------------------------------------- new registers


def test_erasure_needs_48_hour_notice_for_inactivity():
    spec = REGISTERS["erasure"]
    data = {
        "title": "Dormant accounts",
        "reason": "inactivity",
        "notice_sent_at": "2026-05-01T10:00",
        "erased_at": "2026-05-02T10:00",
        "method": "Deleted",
    }
    assert any("48 hours" in p for p in status_problems(spec, "erased", data))
    data["erased_at"] = "2026-05-03T10:00"
    assert status_problems(spec, "erased", data) == []
    assert spec.due_from(data) == "2026-05-03T10:00"
    # Other reasons don't need the notice.
    assert status_problems(spec, "erased", {**data, "reason": "request"}) == []


def test_accepted_gap_needs_an_approver():
    spec = REGISTERS["exceptions"]
    problems = status_problems(spec, "approved", {"title": "No DLP", "reason": "Budget"})
    assert problems and "Approved by" in problems[0]


def test_breach_board_report_draft(app, authed):
    eid = create(authed)
    r = post(
        authed,
        f"/engagements/{eid}/r/breaches",
        {
            "title": "S3 bucket left public",
            "aware_at": "2026-03-01T09:30",
            "breach_type": "disclosure",
        },
    )
    rid = int(r.url.path.rsplit("/", 1)[1])
    assert "Draft Board report" in r.text
    report = authed.get(f"/engagements/{eid}/r/breaches/{rid}/board-report.txt")
    assert report.status_code == 200 and "attachment" in report.headers["content-disposition"]
    assert "S3 bucket left public" in report.text and "Rule 7(2)(b)" in report.text
    assert "[TO FILL IN]" in report.text and "2026-03-04T09:30" in report.text
    assert authed.get(f"/engagements/{eid}/r/breaches/999/board-report.txt").status_code == 404


# ---------------------------------------------------------------- the Board pack


def test_board_pack_zip(app, authed):
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", ALL_YES)
    post(authed, f"/engagements/{eid}/r/systems", {"name": "=HYPERLINK(evil)", "encryption": "yes"})
    authed.post(
        f"/engagements/{eid}/evidence",
        data={"csrf": csrf(authed, "/"), "title": "Policy", "obligation_id": "A-08-5"},
        files={"file": ("policy.txt", b"Access control policy", "text/plain")},
    )
    r = authed.get(f"/engagements/{eid}/board-pack.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    for expected in (
        "00-README.txt",
        "01-obligations-register.csv",
        "02-controls.csv",
        "03-registers/systems.csv",
        "08-evidence/index.csv",
        "09-audit-log.csv",
    ):
        assert expected in names, expected
    assert any(n.startswith("08-evidence/") and n.endswith(".txt") for n in names)
    assert "Acme Pvt Ltd" in z.read("00-README.txt").decode()
    # Spreadsheet formulas are neutralised.
    assert "'=HYPERLINK(evil)" in z.read("03-registers/systems.csv").decode()
    with db(app) as conn:
        assert conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'board_pack_exported'"
        ).fetchone()


def test_board_pack_is_fenced(app, authed, client):
    eid = create(authed)
    add_member(app, "outsider")
    authed.cookies.clear()
    authed.post(
        "/login",
        data={"username": "outsider", "password": MEMBER_PASSWORD, "csrf": csrf(authed)},
    )
    assert authed.get(f"/engagements/{eid}/board-pack.zip").status_code == 404
    assert authed.get(f"/engagements/{eid}/obligations").status_code == 404


def test_trend_keeps_one_row_per_day(app, authed):
    from grc_agent.web import obligation_views

    eid = create(authed)
    with db(app) as conn:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO readiness_history VALUES (?,?,?,?,?,?)", (eid, yesterday, 1, 0, 10, 10)
        )
    authed.get(f"/engagements/{eid}/obligations")
    page = authed.get(f"/engagements/{eid}/obligations").text
    assert "<polyline" in page
    with db(app) as conn:
        assert len(obligation_views.trend(conn, eid)) == 2
