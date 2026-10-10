"""The demo tenant: a separate, seeded sample workspace that looks live."""

import random
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import csrf

from grc_agent.web import db, demo_tenant
from grc_agent.web.app import _summary, create_app


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    return demo_tenant.seed(tmp_path_factory.mktemp("tenant") / "var-demo")


@pytest.fixture
def demo(demo_dir, monkeypatch):
    monkeypatch.setenv("GRC_DEMO_LIVE_SECONDS", "0")
    client = TestClient(create_app(demo_dir))
    token = csrf(client)
    client.post(
        "/login",
        data={
            "username": demo_tenant.DEMO_USER,
            "password": demo_tenant.DEMO_PASSWORD,
            "csrf": token,
        },
    )
    return client


def test_seed_covers_every_pipeline_stage(demo_dir):
    assert demo_tenant.is_demo(demo_dir)
    with db.connect(db.database_target(demo_dir)) as conn:
        stages = [_summary(conn, e)["stage"] for e in conn.execute("SELECT * FROM engagements")]
        evidence = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        oldest = conn.execute("SELECT MIN(at) FROM audit_log").fetchone()[0]
        notes = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert set(stages) == {
        "Intake",
        "Intake submitted",
        "Assessed",
        "In review",
        "Ready to deliver",
        "Delivered",
    }
    assert evidence > 10 and notes > 10
    assert oldest < db.now()[:10]  # history is spread over past weeks


def test_seeds_behind_an_https_proxy(tmp_path, monkeypatch):
    # On a server, cookies are Secure-only; seeding still signs in and adds the clients.
    monkeypatch.setenv("GRC_FORCE_HTTPS", "1")
    monkeypatch.setenv("GRC_PUBLIC_URL", "https://demo.grc-flow.com")
    folder = demo_tenant.seed(tmp_path / "var-demo")
    with db.connect(db.database_target(folder)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM engagements").fetchone()[0] == 6


def test_refuses_to_seed_over_a_real_workspace(tmp_path):
    real = tmp_path / "var"
    real.mkdir()
    db.init_db(real / "grc.db")
    with pytest.raises(SystemExit, match="real workspace"):
        demo_tenant.seed(real, reset=True)
    assert (real / "grc.db").exists()


def test_main_workspace_is_not_a_demo(client):
    assert "Demo tenant" not in client.get("/login").text


def test_demo_banner_login_hint_and_dashboard(demo_dir, demo):
    login = TestClient(create_app(demo_dir)).get("/login").text
    assert demo_tenant.DEMO_PASSWORD in login
    page = demo.get("/").text
    assert "Pinecrest Learning Pvt Ltd" in page
    # The demo-tenant notice is shown on the GRC Analyst page only.
    assert "Demo tenant." not in page
    assert "Demo tenant." in demo.get("/assistant").text
    assert "Ready to deliver" in page and "Top risks" in page


def test_connector_sync_is_simulated(demo):
    page = demo.get("/engagements/3/connectors").text

    action = re.search(r'action="(/engagements/3/connectors/\d+/sync)"', page).group(1)
    page = demo.post(action, data={"csrf": csrf(demo, "/")}).text
    assert "checks collected (demo data)" in page


def test_live_step_makes_colleagues_act(demo_dir):
    with db.connect(db.database_target(demo_dir)) as conn:
        before = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
    rng = random.Random(3)
    done = [demo_tenant.live_step(db.database_target(demo_dir), rng) for _ in range(5)]
    assert all(done)
    with db.connect(db.database_target(demo_dir)) as conn:
        rows = conn.execute("SELECT username FROM audit_log WHERE id > ?", (before,)).fetchall()
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications n WHERE n.id NOT IN "
            "(SELECT notification_id FROM notification_reads WHERE username = 'demo')"
        ).fetchone()[0]
    assert len(rows) == 5 and {r[0] for r in rows} <= set(demo_tenant.COLLEAGUES)
    assert unread > 0


def test_resync_flips_a_check():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE evidence (id INTEGER PRIMARY KEY, connection_id INT, check_key TEXT, "
        "status TEXT, detail TEXT, collected_at TEXT)"
    )
    conn.execute(
        "INSERT INTO evidence (connection_id, check_key, status, detail) "
        "VALUES (1, 'root_mfa', 'fail', 'x')"
    )

    class AlwaysFlip(random.Random):
        def random(self):
            return 0.0

    message = demo_tenant.resync(conn, {"id": 1}, AlwaysFlip())
    assert "root mfa now passing" in message
    assert conn.execute("SELECT status FROM evidence").fetchone()[0] == "pass"


def test_lan_flag_listens_on_all_interfaces(monkeypatch, tmp_path):
    from grc_agent.web import cli

    seen = {}
    monkeypatch.setattr(
        cli, "_serve", lambda d, host, port, r, o, tls: seen.update(host=host, tls=tls) or 0
    )
    monkeypatch.setattr(demo_tenant, "seed", lambda d, reset=False: d)
    cli.main(["demo", "--dir", str(tmp_path / "d"), "--lan", "--https"])
    assert seen == {"host": "0.0.0.0", "tls": True}
    cli.main(["demo", "--dir", str(tmp_path / "d")])
    assert seen == {"host": "127.0.0.1", "tls": False}  # local only, plain HTTP, unless asked
    ip = cli.lan_ip()
    assert ip is None or not ip.startswith("127.")


def test_demo_has_personal_data_to_show(demo_dir, demo):
    with db.connect(db.database_target(demo_dir)) as conn:
        scans = conn.execute("SELECT status FROM scan_jobs").fetchall()
        documented = conn.execute(
            "SELECT COUNT(*) FROM data_inventory WHERE purpose != '' AND owner != ''"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM scan_findings WHERE status = 'pending'"
        ).fetchone()[0]
    assert [s["status"] for s in scans] == ["done", "done"]
    assert documented >= 2 and pending >= 1
    home = demo.get("/").text
    assert "Personal data mapped" in home and "to review" in home


def test_demo_has_privacy_operations_and_controls(demo_dir, demo):
    with db.connect(db.database_target(demo_dir)) as conn:
        regs = dict(conn.execute("SELECT register, COUNT(*) FROM records GROUP BY register"))
        controls = conn.execute("SELECT COUNT(*) FROM controls").fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0]
    for key in ("breaches", "requests", "consent", "vendors", "dpias", "policies", "tasks"):
        assert regs.get(key), key
    assert controls >= 4 and files == 1
    work = demo.get("/work").text
    assert "Lab reports emailed to the wrong patient group" in work and "Overdue" in work


def test_public_demo_switches_off_risky_settings(demo, monkeypatch):
    """demo.grc-flow.com: every visitor is the same admin, so anything that reaches outside
    the demo or locks others out is refused; the client workflow still works."""
    monkeypatch.setenv("GRC_PUBLIC_DEMO", "1")
    page = demo.get("/").text
    assert "Public demo." in page and "resets every night" in page
    token = csrf(demo, "/")
    refused = {
        "/settings/ai": {"provider": "custom", "base_url": "http://169.254.169.254/"},
        "/settings/api-keys": {"name": "x"},
        "/team": {"username": "mallory", "password": "0123456789ab", "role": "admin"},
        f"/team/{demo_tenant.DEMO_USER}/role": {"role": "client"},
        "/engagements/1/connectors": {"connector": "aws", "method": "keys"},
    }
    for path, form in refused.items():
        r = demo.post(path, data={**form, "csrf": token}, headers={"referer": "http://testserver/"})
        assert "This is the public demo" in r.text, path
    with db.connect(demo.app.state.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0
        role = conn.execute(
            "SELECT role FROM users WHERE username = ?", (demo_tenant.DEMO_USER,)
        ).fetchone()[0]
        assert role == "super_admin"  # the demo's only account owns its workspace
        assert not conn.execute("SELECT 1 FROM users WHERE username = 'mallory'").fetchone()
    assert demo.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 403
    # The workflow itself still works.
    r = demo.post("/engagements", data={"client": "Visitor Co", "sector": "SaaS", "csrf": token})
    assert r.status_code == 200 and "Visitor Co" in r.text


def test_reset_empties_the_folder_without_deleting_it(tmp_path, monkeypatch):
    # In Docker the demo folder is a mounted volume: it can be emptied, not removed.
    folder = demo_tenant.seed(tmp_path / "var-demo")
    (folder / "visitor-upload.txt").write_text("x")
    removed = []
    real_rmdir = demo_tenant.shutil.rmtree

    def guard(path, *a, **k):
        assert Path(path) != folder, "tried to delete the mount point itself"
        removed.append(path)
        return real_rmdir(path, *a, **k)

    monkeypatch.setattr(demo_tenant.shutil, "rmtree", guard)
    demo_tenant.seed(folder, reset=True)
    assert folder.exists() and not (folder / "visitor-upload.txt").exists()
    assert demo_tenant.is_demo(folder)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc (Linux)")
def test_seeding_leaves_no_database_file_open(tmp_path):
    # Windows can't delete or replace an open file, so a reset needs every handle closed.
    import os

    demo_tenant.seed(tmp_path / "var-demo")
    open_db = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.endswith("grc.db"):
            open_db.append(target)
    assert open_db == []
