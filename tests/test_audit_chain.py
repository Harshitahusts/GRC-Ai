"""The audit log is hash-chained: an edit made outside the app shows up when checked."""

import csv
import io

from helpers import create

from grc_agent.web import db


def _db(app):
    return db.connect(app.state.db_path)


def test_every_entry_is_sealed_and_the_chain_checks_out(authed, app):
    create(authed)
    with _db(app) as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        assert len(rows) >= 2  # the login and the new engagement
        assert all(r["hash"] for r in rows)
        for before, after in zip(rows, rows[1:], strict=False):
            assert after["prev_hash"] == before["hash"]
        report = db.verify_audit_chain(conn)
    assert report.ok and report.checked == len(rows) and report.last_hash == rows[-1]["hash"]

    page = authed.get("/audit?verify=1").text
    assert "Intact:" in page and report.last_hash in page


def test_a_changed_entry_is_caught(authed, app):
    create(authed)
    with _db(app) as conn:
        target = conn.execute("SELECT id FROM audit_log ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE audit_log SET username = 'someone-else' WHERE id = ?", (target,))
    with _db(app) as conn:
        report = db.verify_audit_chain(conn)
    assert not report.ok and report.broken_id == target
    assert "changed after it was recorded" in report.problem
    assert f"Problem at entry #{target}" in authed.get("/audit?verify=1").text


def test_a_removed_entry_is_caught(authed, app):
    create(authed)
    with _db(app) as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM audit_log ORDER BY id")]
        conn.execute("DELETE FROM audit_log WHERE id = ?", (ids[1],))
    with _db(app) as conn:
        report = db.verify_audit_chain(conn)
    assert not report.ok and report.broken_id == ids[2]
    assert "removed" in report.problem


def test_entries_from_before_sealing_are_counted_not_failed(app):
    with _db(app) as conn:
        # A workspace upgraded from a version without sealing: its entries have no hash.
        conn.execute("UPDATE audit_log SET prev_hash = NULL, hash = NULL")
        conn.execute(
            "INSERT INTO audit_log (at, username, action) VALUES (?,?,?)", (db.now(), "a", "old")
        )
        db.audit(conn, "harshit", "login")
        db.audit(conn, "harshit", "logout")
    with _db(app) as conn:
        report = db.verify_audit_chain(conn)
    assert report.ok and report.before_chain >= 1 and report.checked == 2


def test_csv_export_lets_anyone_recompute_the_chain(authed):
    create(authed)
    text = authed.get("/audit.csv").text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows and rows[0].keys() >= {"prev_hash", "hash"}
    prev = ""
    for r in rows:
        eid = int(r["engagement_id"]) if r["engagement_id"] else None
        assert r["prev_hash"] == prev
        assert r["hash"] == db.entry_hash(
            r["prev_hash"], r["at"], r["username"], eid, r["action"], r["detail"]
        )
        prev = r["hash"]
