"""Registers (tasks, consent, requests, breaches, vendors, DPIAs, policies), controls,
evidence, the work queue, the audit log and team roles."""

from html import escape
from pathlib import Path

import pytest
from helpers import PASSWORD, add_member, create, csrf, login, post

from grc_agent.web import db as webdb
from grc_agent.web.registers import REGISTERS, clean, status_problems


def db(app):
    """The app's own database, SQLite or PostgreSQL."""
    return webdb.connect(app.state.db_path)


def new(client, eid, key, data, **kwargs):
    return post(client, f"/engagements/{eid}/r/{key}", data, **kwargs)


def rid_of(response) -> int:
    return int(response.url.path.rsplit("/", 1)[1])


# ---------------------------------------------------------------- pure rules


def test_every_register_page_renders(authed):
    eid = create(authed)
    for key, spec in REGISTERS.items():
        page = authed.get(f"/engagements/{eid}/r/{key}")
        assert page.status_code == 200, key
        assert escape(spec.title) in page.text
        assert escape(f"No {spec.title.lower()} yet") in page.text
        form = authed.get(f"/engagements/{eid}/r/{key}/new")
        assert form.status_code == 200 and f"Create {spec.singular}" in form.text
    assert authed.get(f"/engagements/{eid}/r/nope").status_code == 404


def test_clean_validates_kinds():
    spec = REGISTERS["breaches"]
    data, errors = clean(spec, {"title": "x", "aware_at": "not a date", "affected_count": "ten"})
    assert "Became aware at: use a date and time." in errors
    assert "People affected (estimate): use a whole number." in errors
    data, errors = clean(
        REGISTERS["requests"], {"summary": "s", "received_on": "2026-01-01", "response_days": "120"}
    )
    assert "at most 90 days" in " ".join(errors) and data["response_days"] == "90"


def test_status_needs_fields():
    spec = REGISTERS["breaches"]
    assert status_problems(spec, "board_intimated", {}) == [
        "Fill in Board told (initial) at before marking it “Board intimated”."
    ]
    assert (
        status_problems(spec, "board_intimated", {"board_intimated_at": "2026-01-01T10:00"}) == []
    )
    vendors = REGISTERS["vendors"]
    assert "valid contract" in " ".join(status_problems(vendors, "active", {"contract": "none"}))


# ---------------------------------------------------------------- web flows


def test_breach_clock_workflow_and_history(app, authed):
    eid = create(authed)
    r = new(
        authed,
        eid,
        "breaches",
        {
            "title": "Laptop with patient list stolen",
            "aware_at": "2026-03-01T09:30",
            "breach_type": "loss",
            "affected_count": "1200",
            "owner": "harshit",
        },
    )
    assert r.status_code == 200 and "BRE-001" in r.text
    rid = rid_of(r)
    with db(app) as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (rid,)).fetchone()
        note = conn.execute(
            "SELECT level, title FROM notifications WHERE category = 'privacy'"
        ).fetchone()
    assert row["due"] == "2026-03-04T09:30"  # 72 hours after awareness
    assert note["level"] == "critical" and "72 hours" in note["title"]
    assert "overdue" in r.text.lower()  # 2026-03-04 is in the past

    base = f"/engagements/{eid}/r/breaches/{rid}"
    blocked = post(authed, f"{base}/status", {"status": "reported"})
    assert "Fill in" in blocked.text and "Board detailed report sent at" in blocked.text
    post(
        authed,
        f"{base}/edit",
        {
            "title": "Laptop with patient list stolen",
            "aware_at": "2026-03-01T09:30",
            "breach_type": "loss",
            "board_intimated_at": "2026-03-01T12:00",
        },
    )
    ok = post(authed, f"{base}/status", {"status": "board_intimated", "note": "Emailed DPB"})
    assert "is now “Board intimated”" in ok.text
    post(authed, f"{base}/comment", {"text": "Police FIR filed."})
    page = authed.get(base).text
    assert "Moved to <strong>Board intimated</strong>" in page and "Emailed DPB" in page
    assert "Police FIR filed." in page and "Changed Board told (initial) at" in page

    # Editing away a field the current status needs is refused.
    r = post(
        authed,
        f"{base}/edit",
        {
            "title": "Laptop with patient list stolen",
            "aware_at": "2026-03-01T09:30",
            "breach_type": "loss",
        },
    )
    assert "Not saved." in r.text


def test_request_needs_identity_and_resolution(app, authed):
    eid = create(authed)
    r = new(
        authed,
        eid,
        "requests",
        {
            "summary": "Customer wants their data erased",
            "request_type": "erasure",
            "received_on": "2026-05-01",
            "response_days": "30",
            "identity": "pending",
        },
    )
    rid = rid_of(r)
    with db(app) as conn:
        assert conn.execute("SELECT due FROM records").fetchone()[0] == "2026-05-31"
    base = f"/engagements/{eid}/r/requests/{rid}"
    assert "Verify the requester" in post(authed, f"{base}/status", {"status": "in_progress"}).text
    assert "Fill in Resolution" in post(authed, f"{base}/status", {"status": "closed"}).text
    post(
        authed,
        f"{base}/edit",
        {
            "summary": "Customer wants their data erased",
            "request_type": "erasure",
            "received_on": "2026-05-01",
            "response_days": "30",
            "identity": "verified",
            "resolution": "Erased from CRM and backups; confirmed by email.",
        },
    )
    assert "is now “Closed”" in post(authed, f"{base}/status", {"status": "closed"}).text


def test_validation_errors_rerender_the_form(authed):
    eid = create(authed)
    r = new(authed, eid, "tasks", {"title": "", "priority": "medium"})
    assert r.status_code == 400 and "What needs doing is required." in r.text


def test_prefilled_task_from_a_control_and_search(authed):
    eid = create(authed)
    form = authed.get(
        f"/engagements/{eid}/r/tasks/new?title=Fix+MFA&obligation=OBL-004&priority=high"
        "&source=Controls"
    ).text
    assert 'value="Fix MFA"' in form and 'value="OBL-004" selected' in form
    r = new(
        authed,
        eid,
        "tasks",
        {"title": "Fix MFA", "priority": "high", "obligation_id": "OBL-004", "source": "Controls"},
    )
    assert "Created from: Controls" in r.text
    assert "Fix MFA" in authed.get(f"/engagements/{eid}/r/tasks?q=OBL-004").text
    assert "Fix MFA" not in authed.get(f"/engagements/{eid}/r/tasks?q=OBL-011").text


def test_records_are_scoped_to_their_engagement(app, authed):
    a, b = create(authed), create(authed)
    rid = rid_of(new(authed, a, "tasks", {"title": "Secret task", "priority": "low"}))
    assert authed.get(f"/engagements/{b}/r/tasks/{rid}").status_code == 404
    assert (
        post(authed, f"/engagements/{b}/r/tasks/{rid}/status", {"status": "done"}).status_code
        == 404
    )
    assert "Secret task" not in authed.get(f"/engagements/{b}/r/tasks?show=all").text


def test_register_csv_export(authed):
    eid = create(authed)
    new(authed, eid, "vendors", {"name": "=cmd()", "contract": "none", "location": "outside"})
    csv_ = authed.get(f"/engagements/{eid}/r/vendors.csv")
    assert csv_.headers["content-type"].startswith("text/csv")
    assert "'=cmd()" in csv_.text and "Outside" not in csv_.text.split("\n")[0]


def test_controls_na_needs_reason_and_admin_and_implemented_needs_proof(app, authed):
    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/controls").text
    assert "Section 8(5)" in page and "not a certification" in page
    r = post(authed, f"/engagements/{eid}/controls/OBL-011", {"status": "not_applicable"})
    assert "Say why" in r.text
    r = post(
        authed,
        f"/engagements/{eid}/controls/OBL-011",
        {"status": "not_applicable", "na_reason": "No users under 18; age-gated at signup"},
    )
    assert "Section 9(1): Not applicable" in r.text
    r = post(authed, f"/engagements/{eid}/controls/OBL-004", {"status": "implemented"})
    assert "attach evidence or describe" in r.text
    r = post(
        authed,
        f"/engagements/{eid}/controls/OBL-004",
        {"status": "implemented", "notes": "MFA on all admin accounts; quarterly review."},
    )
    assert "Section 8(5): Implemented" in r.text
    with db(app) as conn:
        rows = dict(conn.execute("SELECT obligation_id, status FROM controls").fetchall())
    assert rows == {"OBL-011": "not_applicable", "OBL-004": "implemented"}


def test_evidence_upload_validation_download_and_versions(app, authed):
    eid = create(authed)
    pdf = b"%PDF-1.4 fake but signed"
    r = authed.post(
        f"/engagements/{eid}/evidence",
        data={
            "csrf": csrf(authed, "/"),
            "title": "Signed DPA",
            "category": "Contract or DPA",
            "obligation_id": "OBL-012",
        },
        files={"file": ("dpa.pdf", pdf, "application/pdf")},
    )
    assert "Uploaded Signed DPA." in r.text
    bad = authed.post(
        f"/engagements/{eid}/evidence",
        data={"csrf": csrf(authed, "/")},
        files={"file": ("evil.pdf", b"MZ\x90 not a pdf", "application/pdf")},
    )
    assert "don&#39;t match its .pdf extension" in bad.text
    exe = authed.post(
        f"/engagements/{eid}/evidence",
        data={"csrf": csrf(authed, "/")},
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
    )
    assert "Upload a PDF" in exe.text
    with db(app) as conn:
        f = conn.execute("SELECT * FROM evidence_files").fetchone()
    stored = Path(app.state.data_dir) / "evidence" / f["stored_name"]
    assert stored.read_bytes() == pdf and f["sha256"] and f["filename"] == "dpa.pdf"

    d = authed.get(f"/engagements/{eid}/evidence/{f['id']}/download")
    assert d.content == pdf and d.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in d.headers["content-disposition"]
    other = create(authed)
    assert authed.get(f"/engagements/{other}/evidence/{f['id']}/download").status_code == 404

    authed.post(
        f"/engagements/{eid}/evidence",
        data={"csrf": csrf(authed, "/"), "replaces_id": str(f["id"]), "title": "Signed DPA"},
        files={"file": ("dpa-v2.pdf", b"%PDF-1.7 v2", "application/pdf")},
    )
    with db(app) as conn:
        rows = conn.execute("SELECT version, status FROM evidence_files ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "superseded"), (2, "current")]

    # Evidence now counts for the control.
    assert "1 file" in authed.get(f"/engagements/{eid}/controls").text
    authed.cookies.clear()
    r = authed.get(f"/engagements/{eid}/evidence/{f['id']}/download", follow_redirects=False)
    assert r.status_code == 303


def test_attach_evidence_to_a_record_and_delete(app, authed):
    eid = create(authed)
    rid = rid_of(
        new(
            authed,
            eid,
            "tasks",
            {"title": "Publish notice", "priority": "high", "obligation_id": "OBL-001"},
        )
    )
    authed.post(
        f"/engagements/{eid}/evidence",
        data={
            "csrf": csrf(authed, "/"),
            "record_id": str(rid),
            "title": "Screenshot",
            "back": f"/engagements/{eid}/r/tasks/{rid}",
        },
        files={"file": ("shot.png", b"\x89PNG\r\n rest", "image/png")},
    )
    page = authed.get(f"/engagements/{eid}/r/tasks/{rid}").text
    assert "Screenshot" in page and "Attached evidence: Screenshot" in page
    with db(app) as conn:
        f = conn.execute("SELECT * FROM evidence_files").fetchone()
    assert f["obligation_id"] == "OBL-001"  # inherited from the task
    post(authed, f"/engagements/{eid}/evidence/{f['id']}/delete")
    assert not (Path(app.state.data_dir) / "evidence" / f["stored_name"]).exists()
    with db(app) as conn:
        assert "evidence_deleted" in [r[0] for r in conn.execute("SELECT action FROM audit_log")]


def test_work_queue_and_audit_log(authed):
    eid = create(authed)
    new(
        authed,
        eid,
        "breaches",
        {"title": "Misdirected email", "aware_at": "2020-01-01T10:00", "owner": "harshit"},
    )
    new(authed, eid, "tasks", {"title": "Train staff", "priority": "low", "owner": "someone"})
    work = authed.get("/work").text
    assert "Misdirected email" in work and "Train staff" in work and "Overdue" in work
    mine = authed.get("/work?mine=1").text
    assert "Misdirected email" in mine and "Train staff" not in mine
    audit = authed.get("/audit?action=record_created").text
    assert "record_created" in audit and "Acme Pvt Ltd" in audit
    assert (
        "engagement_created"
        not in authed.get("/audit?action=record_created").text.split("<tbody>")[1]
    )


def test_guest_sees_only_the_engagement_given_and_viewers_read_only(app, authed, client):
    add_member(app, "vera", team_role="manager", password="client-pass-123")
    eid = create(authed)
    authed.cookies.clear()
    token = csrf(client, "/login?as=company")
    client.post("/login", data={"username": "vera", "password": "client-pass-123", "csrf": token})
    # Not given the engagement yet: it doesn't exist for her.
    assert client.get(f"/engagements/{eid}").status_code == 404
    assert client.get(f"/engagements/{eid}/r/tasks").status_code == 404
    assert "Acme" not in client.get("/engagements").text
    with db(app) as conn:
        conn.execute(
            "INSERT INTO engagement_access (engagement_id, username, granted_by, granted_at) "
            "VALUES (?, 'vera', 'harshit', '2026-01-01')",
            (eid,),
        )
    assert client.get(f"/engagements/{eid}/r/tasks").status_code == 200
    r = post(client, f"/engagements/{eid}/r/tasks", {"title": "x", "priority": "low"})
    assert r.status_code == 200  # a manager works the registers
    with db(app) as conn:
        conn.execute("UPDATE users SET team_role = 'viewer' WHERE username = 'vera'")
    r = post(client, f"/engagements/{eid}/r/tasks", {"title": "y", "priority": "low"})
    assert r.status_code == 403 and "read-only" in r.text


def test_only_the_lead_marks_not_applicable(app, authed, client):
    add_member(app, "mona", team_role="manager", password="client-pass-123")
    eid = create(authed)
    with db(app) as conn:
        conn.execute(
            "INSERT INTO engagement_access (engagement_id, username, granted_by, granted_at) "
            "VALUES (?, 'mona', 'harshit', '2026-01-01')",
            (eid,),
        )
    authed.cookies.clear()
    token = csrf(client, "/login?as=company")
    client.post("/login", data={"username": "mona", "password": "client-pass-123", "csrf": token})
    r = post(
        client,
        f"/engagements/{eid}/controls/OBL-011",
        {"status": "not_applicable", "na_reason": "No kids"},
    )
    assert "Only the engagement lead" in r.text


def test_engagement_nav_groups_and_counts(authed):
    eid = create(authed)
    new(
        authed,
        eid,
        "requests",
        {"summary": "Access request", "request_type": "access", "received_on": "2020-01-01"},
    )
    page = authed.get(f"/engagements/{eid}/r/tasks").text
    for label in (
        "Privacy operations",
        "Compliance",
        "Risk",
        "Consent",
        "Breaches",
        "Controls",
        "Evidence",
        "Policies",
        "Vendors",
        "DPIA",
    ):
        assert label in page
    assert 'class="mcount late" title="1 overdue"' in page


def test_new_tables_are_catalogued(authed):
    assert "Uncatalogued" not in authed.get("/data-manager").text


@pytest.mark.parametrize("path", ["/work", "/audit", "/team"])
def test_org_pages_need_login(client, path):
    assert client.get(path, follow_redirects=False).status_code == 303


def test_password_constant_is_long_enough():
    assert len(PASSWORD) >= 10 and login  # helpers used above
