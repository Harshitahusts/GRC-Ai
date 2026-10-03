"""API keys and the read-only MCP endpoint other AI apps connect to."""

import json
import re

import pytest
from helpers import ALL_YES, create, post

from grc_agent.web import db


def _make_key(authed, name="Claude Desktop"):
    page = post(authed, "/settings/api-keys", {"name": name}).text
    key = re.search(r'id="new-key">(grcf_[^<]+)<', page).group(1)
    assert "won't be shown again" in page
    # Only once: the next visit no longer shows it.
    assert key not in authed.get("/settings/api-keys").text
    return key


def _rpc(client, key, method, params=None, rid=1, **headers):
    body = {"jsonrpc": "2.0", "method": method, "id": rid}
    if params is not None:
        body["params"] = params
    if rid is None:
        body.pop("id")
    hdrs = {"Authorization": f"Bearer {key}", **headers} if key else headers
    return client.post("/mcp", content=json.dumps(body), headers=hdrs)


@pytest.fixture
def mcp(authed):
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", {**ALL_YES, "q_Q-BREACH": "no", "action": "submit"})
    post(authed, f"/engagements/{eid}/assess")
    return authed, _make_key(authed), eid


def test_keys_are_stored_only_as_a_hash(mcp, app):
    _, key, _ = mcp
    with db.connect(app.state.db_path) as conn:
        row = conn.execute("SELECT * FROM api_keys").fetchone()
    assert key not in json.dumps(dict(row)) and row["key_hash"] and row["hint"].startswith("grcf_")


def test_handshake_and_read_only_tool_list(mcp):
    client, key, _ = mcp
    init = _rpc(client, key, "initialize", {"protocolVersion": "2025-06-18"}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "grc-flow"
    assert "DPDPA" in init["result"]["instructions"]
    assert _rpc(client, key, "notifications/initialized", rid=None).status_code == 202

    tools = _rpc(client, key, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"list_engagements", "get_findings", "get_readiness_plan"} <= names
    assert "create_task" not in names  # nothing that writes
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


def test_calling_a_tool_reads_live_data_and_is_audited(mcp, app):
    client, key, eid = mcp
    out = _rpc(
        client,
        key,
        "tools/call",
        {"name": "get_findings", "arguments": {"engagement_id": eid, "status": "gap"}},
    ).json()["result"]
    assert not out["isError"]
    findings = json.loads(out["content"][0]["text"])["findings"]
    assert any(f["obligation_id"] == "OBL-005" for f in findings)

    bad = _rpc(client, key, "tools/call", {"name": "get_findings", "arguments": {"x": 1}}).json()
    assert bad["result"]["isError"]
    assert (
        _rpc(client, key, "tools/call", {"name": "create_task"}).json()["error"]["code"] == -32602
    )
    assert _rpc(client, key, "resources/list").json()["error"]["code"] == -32601

    with db.connect(app.state.db_path) as conn:
        rows = conn.execute(
            "SELECT username, engagement_id, detail FROM audit_log WHERE action = 'mcp_tool_called'"
        ).fetchall()
        used = conn.execute("SELECT last_used_at FROM api_keys").fetchone()[0]
    assert rows[0]["username"] == "harshit" and rows[0]["engagement_id"] == eid
    assert used


def test_no_key_a_wrong_key_or_a_revoked_key_is_refused(mcp, app):
    client, key, _ = mcp
    assert _rpc(client, None, "tools/list").status_code == 401
    assert _rpc(client, "grcf_wrong", "tools/list").status_code == 401
    # The browser session alone is not enough: the endpoint takes keys only.
    assert (
        client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    )

    with db.connect(app.state.db_path) as conn:
        kid = conn.execute("SELECT id FROM api_keys").fetchone()[0]
    post(client, f"/settings/api-keys/{kid}/revoke")
    assert _rpc(client, key, "tools/list").status_code == 401
    assert "Revoked" in client.get("/settings/api-keys").text


def test_cross_site_requests_and_bad_messages(mcp):
    client, key, _ = mcp
    r = _rpc(client, key, "ping", Origin="https://evil.example")
    assert r.status_code == 403
    assert _rpc(client, key, "ping", Origin="http://testserver").status_code == 200
    bad = client.post("/mcp", content="not json", headers={"Authorization": f"Bearer {key}"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700
    batch = client.post("/mcp", content="[]", headers={"Authorization": f"Bearer {key}"})
    assert batch.status_code == 400
    assert client.get("/mcp").status_code == 405
