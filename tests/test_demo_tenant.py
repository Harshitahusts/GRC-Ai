"""The demo tenant: a separate, seeded sample workspace that looks live."""

import random
import re

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
    with db.connect(demo_dir / "grc.db") as conn:
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
    assert "Demo tenant." in page and "Pinecrest Learning Pvt Ltd" in page
    assert "Ready to deliver" in page and "Top risks" in page


def test_connector_sync_is_simulated(demo):
    page = demo.get("/engagements/3/connectors").text

    action = re.search(r'action="(/engagements/3/connectors/\d+/sync)"', page).group(1)
    page = demo.post(action, data={"csrf": csrf(demo, "/")}).text
    assert "checks collected (demo data)" in page


def test_live_step_makes_colleagues_act(demo_dir):
    with db.connect(demo_dir / "grc.db") as conn:
        before = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
    rng = random.Random(3)
    done = [demo_tenant.live_step(demo_dir / "grc.db", rng) for _ in range(5)]
    assert all(done)
    with db.connect(demo_dir / "grc.db") as conn:
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
