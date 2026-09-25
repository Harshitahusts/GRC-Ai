"""Notifications: built from audit events, read state per user, live count."""

import io

import pytest
from fastapi.testclient import TestClient
from helpers import ALL_YES, PASSWORD, create, csrf, post

from grc_agent.web import cli as web_cli


@pytest.fixture
def priya(app, monkeypatch):
    """A second consultant, logged in on their own client."""
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    data_dir = str(app.state.data_dir)
    assert web_cli.main(["--data-dir", data_dir, "adduser", "priya", "--password-stdin"]) == 0
    client = TestClient(app)
    token = csrf(client)
    client.post("/login", data={"username": "priya", "password": PASSWORD, "csrf": token})
    return client


def unread(client):
    return client.get("/notifications/unread.json").json()["unread"]


def test_your_own_actions_arrive_read_others_see_them_unread(authed, priya):
    mine, theirs = unread(authed), unread(priya)
    create(authed)
    assert unread(authed) == mine
    assert unread(priya) == theirs + 1
    page = priya.get("/notifications?show=unread").text
    assert "New engagement: Acme Pvt Ltd" in page and "is-unread" in page


def test_workflow_events_become_notifications(authed, priya):
    eid = create(authed)
    post(authed, f"/engagements/{eid}/intake", {**ALL_YES, "action": "submit"})
    post(authed, f"/engagements/{eid}/assess", {"mode": "rules"})
    post(authed, f"/engagements/{eid}/intake", {**ALL_YES, "q_Q-NOTICE": "no", "action": "save"})
    page = priya.get("/notifications").text
    assert "Acme Pvt Ltd: intake submitted" in page
    assert "Acme Pvt Ltd: assessment complete (13 findings)" in page
    assert "intake changed after the assessment. Re-run it." in page
    # Filters
    only = priya.get("/notifications?category=assessment").text
    assert "assessment complete" in only and "New engagement" not in only
    warnings = priya.get("/notifications?level=warning").text
    assert "Re-run it." in warnings and "assessment complete" not in warnings


def test_opening_marks_read_and_follows_the_link(authed, priya):
    eid = create(authed)
    before = unread(priya)
    feed = priya.get("/notifications/unread.json?after=0").json()
    nid = feed["latest"]
    response = priya.get(f"/notifications/{nid}/open", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/engagements/{eid}"
    assert unread(priya) == before - 1


def test_mark_some_read(authed, priya):
    create(authed)
    feed = priya.get("/notifications/unread.json").json()
    before = feed["unread"]
    post(priya, "/notifications/read", {"ids": str(feed["latest"])})
    assert unread(priya) == before - 1


def test_mark_all_read_and_safe_redirect(authed, priya):
    create(authed)
    assert unread(priya) > 0
    evil = {"back": "//evil.example"}
    response = post(priya, "/notifications/read", evil, follow_redirects=False)
    assert response.headers["location"] == "/notifications"
    assert unread(priya) == 0
    assert "No unread notifications." in priya.get("/notifications?show=unread").text


def test_live_feed_returns_only_new_items(authed, priya):
    latest = priya.get("/notifications/unread.json").json()["latest"]
    create(authed)
    feed = priya.get(f"/notifications/unread.json?after={latest}").json()
    assert [n["title"] for n in feed["new"]] == ["New engagement: Acme Pvt Ltd"]
    assert feed["new"][0]["link"].startswith("/engagements/")


def test_failed_login_is_a_security_alert_for_everyone(client, authed):
    before = unread(authed)
    token = csrf(client)
    client.post("/login", data={"username": "harshit", "password": "wrong", "csrf": token})
    page = authed.get("/notifications?category=security").text
    assert "Failed login attempt for &#39;harshit&#39;" in page
    assert unread(authed) == before + 1  # even though it was "your" account


def test_bell_shows_the_unread_count(authed, priya):
    create(authed)
    page = priya.get("/").text
    assert 'id="bell-count"' in page and 'id="bell-count" hidden' not in page


def test_notifications_need_login(client):
    assert client.get("/notifications", follow_redirects=False).status_code in (302, 303, 401)
    assert client.get("/notifications/unread.json", follow_redirects=False).status_code != 200
