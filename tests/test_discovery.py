"""Personal data discovery: detectors, file parsing, the scan job, review and inventory."""

from datetime import date
from pathlib import Path

import pytest
from helpers import create, csrf, post

from grc_agent.discovery import catalog, detectors, scanner
from grc_agent.discovery.engine import BUILTIN, presidio_installed
from grc_agent.discovery.scanner import ScanInputError, read_table, scan_table, shape
from grc_agent.web import db as webdb

SAMPLES = Path(__file__).resolve().parents[1] / "src" / "grc_agent" / "discovery" / "samples"
CUSTOMERS = SAMPLES / "customers_sample.csv"
PATIENTS = SAMPLES / "clinic_patients_sample.json"


@pytest.fixture(autouse=True)
def _builtin_scanner(monkeypatch):
    """Tests use the built-in rules so results don't depend on Presidio being installed."""
    monkeypatch.setenv("GRC_SCANNER", "builtin")


# ---------------------------------------------------------------- detectors


@pytest.mark.parametrize(
    ("value", "entity"),
    [
        ("2830 1661 3185", "IN_AADHAAR"),
        ("ABCPE1234F", "IN_PAN"),
        ("27AAPFU0939F1ZV", "IN_GSTIN"),
        ("+91 98765 43210", "PHONE_NUMBER"),
        ("09876543210", "PHONE_NUMBER"),
        ("priya.sharma@example.in", "EMAIL_ADDRESS"),
        ("4111 1111 1111 1111", "CREDIT_CARD"),
        ("priya99@okaxis", "UPI_ID"),
        ("49.36.12.200", "IP_ADDRESS"),
        ("Call me on 9876543210 after 6", "PHONE_NUMBER"),
    ],
)
def test_detectors_find(value, entity):
    assert entity in detectors.detect(value)


@pytest.mark.parametrize(
    ("value", "entity"),
    [
        ("2830 1661 3186", "IN_AADHAAR"),  # wrong Verhoeff check digit
        ("1234 5678 9012", "IN_AADHAAR"),  # Aadhaar never starts with 0 or 1
        ("27AAPFU0939F1ZA", "IN_GSTIN"),  # wrong check character
        ("4111 1111 1111 1112", "CREDIT_CARD"),  # fails Luhn
        ("12345", "PHONE_NUMBER"),
        ("priya.sharma@example.in", "UPI_ID"),  # an email is not a UPI ID
        ("SKU-TSHIRT-M", "IN_PAN"),
    ],
)
def test_detectors_reject(value, entity):
    assert entity not in detectors.detect(value)


def test_aadhaar_is_not_also_reported_as_a_phone_or_card():
    assert detectors.detect("2830 1661 3185") == {"IN_AADHAAR": 0.9}


@pytest.mark.parametrize(
    ("column", "entity"),
    [
        ("customer_email", "EMAIL_ADDRESS"),
        ("FullName", "PERSON"),
        ("Mobile No", "PHONE_NUMBER"),
        ("date_of_birth", "DATE_OF_BIRTH"),
        ("aadhaar_no", "IN_AADHAAR"),
        ("blood_group", "HEALTH"),
        ("product_name", None),
        ("company_name", None),
        ("order_state", None),
        ("page_views", None),
    ],
)
def test_column_name_hints(column, entity):
    assert catalog.hint_for(column) == entity


def test_shape_hides_every_letter_and_digit():
    assert shape("Priya S. +91 98765") == "Xxxxx X. +99 99999"
    assert shape("x" * 50).endswith("…")


# ---------------------------------------------------------------- parsing


def test_read_csv_sniffs_the_delimiter():
    t = read_table("x.csv", b"name;email\nPriya;p@example.in\n;\nRohan;r@example.in\n")
    assert t.kind == "csv"
    assert t.columns == ["name", "email"]
    assert t.rows == 2  # blank rows are skipped
    assert t.samples["email"] == ["p@example.in", "r@example.in"]


def test_read_json_wrapped_nested_and_lines():
    t = read_table("p.json", PATIENTS.read_bytes())
    assert "contact.email" in t.columns and "contact.phone" in t.columns
    assert t.rows == 25
    lines = b'{"email": "a@example.in"}\n{"email": "b@example.in", "tags": ["x", "y"]}\n'
    t = read_table("p.jsonl", lines)
    assert t.columns == ["email", "tags"] and t.samples["tags"] == ["x, y"]


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("a.csv", b"", "empty"),
        ("a.xlsx", b"x", ".csv or .json"),
        ("a.csv", b"name,email\n", "no records"),
        ("a.json", b'{"a": 1}', "array of records"),
        ("a.json", b"[1, 2]", "list of objects"),
        ("a.json", b"{not json", "isn't valid"),
    ],
)
def test_read_table_rejects_bad_files(name, data, message):
    with pytest.raises(ScanInputError, match=message):
        read_table(name, data)


def test_read_table_limits_size(monkeypatch):
    monkeypatch.setattr(scanner, "MAX_BYTES", 10)
    with pytest.raises(ScanInputError, match="over"):
        read_table("a.csv", b"name\n" + b"x\n" * 10)


def test_sampling_keeps_rows_spread_out(monkeypatch):
    monkeypatch.setattr(scanner, "SAMPLE_ROWS", 10)
    data = "n\n" + "\n".join(str(i) for i in range(1000))
    t = read_table("a.csv", data.encode())
    assert t.rows == 1000
    assert len(t.samples["n"]) == 10
    assert t.samples["n"][-1] == "900"


# ---------------------------------------------------------------- analysis


def test_scan_of_the_customer_sample():
    t = read_table(CUSTOMERS.name, CUSTOMERS.read_bytes())
    results = {r.column: r for r in scan_table(t, BUILTIN, today=date(2026, 9, 1))}
    assert results["aadhaar_no"].primary == "IN_AADHAAR"
    assert results["aadhaar_no"].confidence == "high" and results["aadhaar_no"].risk == "high"
    assert results["pan"].primary == "IN_PAN"
    assert results["email"].primary == "EMAIL_ADDRESS" and results["email"].match_ratio == 1.0
    assert results["mobile"].primary == "PHONE_NUMBER"
    assert results["upi_id"].primary == "UPI_ID"
    assert results["full_name"].primary == "PERSON"
    assert results["signup_ip"].primary == "IP_ADDRESS"
    dob = results["date_of_birth"]
    assert dob.minors > 0 and dob.category == "Children" and dob.risk == "high"
    for column in ("customer_id", "order_total_inr", "last_order_sku"):
        assert column not in results
    # High-risk fields come first.
    ordered = list(results)
    assert ordered.index("aadhaar_no") < ordered.index("email") < ordered.index("pincode")


def test_scan_of_the_patient_sample_flags_health_and_children():
    t = read_table(PATIENTS.name, PATIENTS.read_bytes())
    results = {r.column: r for r in scan_table(t, BUILTIN)}
    assert results["diagnosis"].category == "Health"
    assert results["blood_group"].category == "Health"
    assert results["age"].minors > 0 and results["age"].category == "Children"
    assert "ward" not in results and "visit_date" not in results


def test_progress_is_reported_per_column():
    t = read_table("a.csv", b"email,n\na@example.in,1\n")
    seen = []
    scan_table(t, BUILTIN, progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 2), (2, 2)]


@pytest.mark.skipif(not presidio_installed(), reason="Presidio isn't installed")
def test_presidio_finds_names_in_free_text(monkeypatch):
    from grc_agent.discovery import engine as engine_mod

    monkeypatch.delenv("GRC_SCANNER")
    monkeypatch.setattr(engine_mod, "_cached", None)
    eng = engine_mod.get_engine()
    if eng.name != "presidio":
        pytest.skip("No spaCy English model installed")
    t = read_table(CUSTOMERS.name, CUSTOMERS.read_bytes())
    results = {r.column: r for r in scan_table(t, eng)}
    assert results["full_name"].confidence == "high"
    assert results["support_notes"].primary == "PERSON"
    assert "last_order_sku" not in results


# ---------------------------------------------------------------- web flow


def upload(client, eid, path=CUSTOMERS, name="Shopify customers", **kwargs):
    return client.post(
        f"/engagements/{eid}/discovery/scan",
        data={"csrf": csrf(client, "/"), "source_name": name},
        files={"file": (path.name, path.read_bytes(), "text/csv")},
        **kwargs,
    )


def db(app):
    """The app's own database, SQLite or PostgreSQL."""
    return webdb.connect(app.state.db_path)


def test_discovery_needs_login_and_an_agent_engagement(client, authed):
    eid = create(authed)
    manual = create(authed, mode="manual")
    assert authed.get(f"/engagements/{manual}/discovery").status_code == 400
    authed.cookies.clear()
    r = authed.get(f"/engagements/{eid}/discovery", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_upload_scan_review_and_inventory(app, authed):
    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/discovery")
    assert "No scans yet" in page.text and "Built-in rules" in page.text

    r = upload(authed, eid)
    assert r.status_code == 200
    assert "Scanning customers_sample.csv: 40 records, 15 fields" in r.text
    with db(app) as conn:
        job = conn.execute("SELECT * FROM scan_jobs").fetchone()
        assert job["status"] == "done" and job["progress"] == 100 and job["engine"] == "builtin"
        assert job["rows"] == 40 and job["personal_fields"] >= 9
        findings = conn.execute("SELECT * FROM scan_findings").fetchall()
    assert {f["status"] for f in findings} == {"pending"}
    assert "aadhaar_no" in r.text and "Aadhaar number" in r.text
    assert "belong to someone under 18" in r.text

    # Confirm the high-confidence findings in one go.
    r = post(authed, f"/engagements/{eid}/discovery/confirm-high")
    assert "Confirmed" in r.text
    with db(app) as conn:
        inv = {i["field"]: i for i in conn.execute("SELECT * FROM data_inventory")}
    assert {"aadhaar_no", "pan", "email", "mobile", "upi_id"} <= set(inv)
    assert "full_name" not in inv  # medium confidence: still waiting for a person

    # Confirm one by hand; children's data carries over.
    with db(app) as conn:
        dob = conn.execute(
            "SELECT id FROM scan_findings WHERE column_name = 'date_of_birth'"
        ).fetchone()
    r = post(authed, f"/engagements/{eid}/discovery/findings/{dob['id']}", {"action": "confirm"})
    assert "Added Shopify customers · date_of_birth to the data inventory" in r.text
    with db(app) as conn:
        row = conn.execute("SELECT * FROM data_inventory WHERE field = 'date_of_birth'").fetchone()
    assert row["children"] == 1 and row["principals"] == "Children"

    # The inventory lists gaps until each record is documented.
    page = authed.get(f"/engagements/{eid}/inventory")
    assert "5 missing" in page.text or "4 missing" in page.text
    assert "Section 9(1)" in page.text
    item = inv["email"]["id"]
    r = post(
        authed,
        f"/engagements/{eid}/inventory/{item}",
        {
            "purpose": "Send order updates and invoices",
            "principals": "Customers",
            "legal_basis": "consent",
            "retention": "3 years after the last order",
            "owner": "Head of CX",
            "storage_location": "AWS ap-south-1",
            "recipients": "",
        },
    )
    assert "Saved Shopify customers · email." in r.text and "Still missing" not in r.text
    with db(app) as conn:
        assert (
            conn.execute("SELECT legal_basis FROM data_inventory WHERE id = ?", (item,)).fetchone()[
                0
            ]
            == "consent"
        )

    # The dashboard counts it.
    home = authed.get("/")
    assert "Personal data mapped" in home.text
    assert "personal data finding" in home.text  # remaining pending ones need attention

    # Exports.
    csv_ = authed.get(f"/engagements/{eid}/inventory.csv")
    assert csv_.headers["content-type"].startswith("text/csv")
    assert "Send order updates and invoices" in csv_.text and "OBL-011" in csv_.text
    assert "aadhaar_no" in authed.get(f"/engagements/{eid}/discovery/findings.csv").text


def test_raw_values_are_never_stored(app, authed):
    eid = create(authed)
    upload(authed, eid)
    post(authed, f"/engagements/{eid}/discovery/confirm-high")
    raw = CUSTOMERS.read_text().splitlines()[1].split(",")
    # Email, Aadhaar, PAN and UPI of the first customer.
    secrets = [raw[2], raw[5], raw[6], raw[10]]
    # Every value in every table, whichever database holds them.
    from grc_agent.web import datamanager

    with db(app) as conn:
        dump = " ".join(
            str(v)
            for t in datamanager._tables(conn)
            for row in conn.execute(f"SELECT * FROM {t}").fetchall()
            for v in row
        )
    for value in secrets:
        assert value not in dump, value
    if not webdb.is_postgres(app.state.db_path):
        blob = Path(app.state.db_path).read_bytes()
        for wal in Path(app.state.db_path).parent.glob("*.db-wal"):
            blob += wal.read_bytes()
        for value in secrets:
            assert value.encode() not in blob, value
    assert not list(Path(app.state.data_dir).glob("*.csv"))


def test_reject_needs_a_reason_and_reopen_undoes_a_confirmation(app, authed):
    eid = create(authed)
    upload(authed, eid)
    with db(app) as conn:
        fid = conn.execute("SELECT id FROM scan_findings WHERE column_name = 'pan'").fetchone()[0]
    r = post(authed, f"/engagements/{eid}/discovery/findings/{fid}", {"action": "reject"})
    assert "Say why" in r.text
    post(authed, f"/engagements/{eid}/discovery/findings/{fid}", {"action": "confirm"})
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_inventory").fetchone()[0] == 1
    post(authed, f"/engagements/{eid}/discovery/findings/{fid}", {"action": "reopen"})
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_inventory").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM scan_findings WHERE id = ?", (fid,)).fetchone()[0]
            == "pending"
        )
    r = post(
        authed,
        f"/engagements/{eid}/discovery/findings/{fid}",
        {"action": "reject", "note": "Test column, filled with dummy values"},
    )
    assert "Marked Shopify customers · pan as not personal data" in r.text
    with db(app) as conn:
        audit = [a[0] for a in conn.execute("SELECT action FROM audit_log")]
    assert {"finding_confirmed", "finding_pending", "finding_rejected"} <= set(audit)


def test_rescan_replaces_pending_findings_and_keeps_confirmed_ones(app, authed):
    eid = create(authed)
    upload(authed, eid)
    post(authed, f"/engagements/{eid}/discovery/confirm-high")
    with db(app) as conn:
        before = conn.execute("SELECT COUNT(*) FROM data_inventory").fetchone()[0]
    upload(authed, eid)
    with db(app) as conn:
        rows = conn.execute(
            "SELECT scan_id, column_name, status FROM scan_findings ORDER BY id"
        ).fetchall()
        inventory = conn.execute("SELECT COUNT(*) FROM data_inventory").fetchone()[0]
    first = {r["column_name"]: r["status"] for r in rows if r["scan_id"] == 1}
    second = {r["column_name"]: r["status"] for r in rows if r["scan_id"] == 2}
    assert first["full_name"] == "superseded" and first["email"] == "confirmed"
    assert second["email"] == "confirmed" and second["full_name"] == "pending"
    assert inventory == before == 6  # no duplicates


def test_tenant_isolation_between_engagements(app, authed):
    a = create(authed)
    b = create(authed)
    upload(authed, a)
    with db(app) as conn:
        fid = conn.execute("SELECT id FROM scan_findings").fetchone()[0]
    r = post(authed, f"/engagements/{b}/discovery/findings/{fid}", {"action": "confirm"})
    assert r.status_code == 404
    assert authed.get(f"/engagements/{b}/discovery/scans/1.json").status_code == 404
    assert authed.get(f"/engagements/{a}/discovery/scans/1.json").json()["status"] == "done"
    assert "aadhaar_no" not in authed.get(f"/engagements/{b}/discovery?show=all").text


def test_bad_upload_is_explained_and_nothing_is_queued(app, authed):
    eid = create(authed)
    r = authed.post(
        f"/engagements/{eid}/discovery/scan",
        data={"csrf": csrf(authed, "/")},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert "Couldn&#39;t scan notes.txt: Upload a .csv or .json file." in r.text
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scan_jobs").fetchone()[0] == 0


def test_a_crashing_scan_is_marked_failed_and_notified(app, authed, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("detector bug")

    monkeypatch.setattr("grc_agent.web.discovery_views.scan_table", boom)
    eid = create(authed)
    r = upload(authed, eid)
    assert "Failed" in r.text and "Upload the file again" in r.text
    with db(app) as conn:
        assert conn.execute("SELECT status FROM scan_jobs").fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 0
        note = conn.execute(
            "SELECT level, title FROM notifications WHERE category = 'discovery'"
        ).fetchone()
    assert note["level"] == "serious" and "failed" in note["title"]


def test_children_scan_raises_a_warning_notification(app, authed):
    eid = create(authed)
    upload(authed, eid, PATIENTS, "Clinic records")
    with db(app) as conn:
        note = conn.execute(
            "SELECT level, title FROM notifications WHERE category = 'discovery'"
        ).fetchone()
    assert note["level"] == "warning" and "Section 9" in note["title"]


def test_manual_inventory_records_and_csv_is_formula_safe(app, authed):
    eid = create(authed)
    r = post(
        authed,
        f"/engagements/{eid}/inventory",
        {"source_name": "=HYPERLINK(1)", "field": "employee_pan", "kind": "IN_PAN"},
    )
    assert "Added" in r.text
    again = post(
        authed,
        f"/engagements/{eid}/inventory",
        {"source_name": "=HYPERLINK(1)", "field": "employee_pan", "kind": "IN_PAN"},
    )
    assert "already in the inventory" in again.text
    bad = post(
        authed, f"/engagements/{eid}/inventory", {"source_name": "x", "field": "y", "kind": "NOPE"}
    )
    assert "Give the system" in bad.text
    assert "'=HYPERLINK(1)" in authed.get(f"/engagements/{eid}/inventory.csv").text
    with db(app) as conn:
        iid = conn.execute("SELECT id FROM data_inventory").fetchone()[0]
    r = post(authed, f"/engagements/{eid}/inventory/{iid}", {"legal_basis": "whatever"})
    assert r.status_code == 400
    r = post(authed, f"/engagements/{eid}/inventory/{iid}/delete")
    assert "Removed" in r.text
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_inventory").fetchone()[0] == 0


def test_delivered_engagement_is_read_only(app, authed):
    eid = create(authed)
    upload(authed, eid)
    with db(app) as conn:
        conn.execute("UPDATE engagements SET delivered_at = '2026-09-01T00:00:00+00:00'")
    assert upload(authed, eid).status_code == 400
    page = authed.get(f"/engagements/{eid}/discovery")
    assert "no new scans" in page.text


def test_new_tables_are_catalogued_in_the_data_manager(authed):
    page = authed.get("/data-manager")
    assert "Uncatalogued" not in page.text
    assert "scan_findings" in page.text and "data_inventory" in page.text


def test_sample_files_download_behind_login(client, authed):
    r = authed.get("/discovery/samples/customers_sample.csv")
    assert r.status_code == 200 and r.text.startswith("customer_id,full_name")
    assert authed.get("/discovery/samples/..%2Fengine.py").status_code == 404
    authed.cookies.clear()
    assert (
        authed.get("/discovery/samples/customers_sample.csv", follow_redirects=False).status_code
        == 303
    )
