"""DPDP training: the switch, importing employees, personal links, the no-skip watch
clock, quizzes (70% passes, failing means watching again), policy acceptance and the
leaderboard."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from helpers import create, csrf, post

from grc_agent import training
from grc_agent.connectors import base, hr
from grc_agent.web import db as webdb
from grc_agent.web import training_views


def db(app):
    return webdb.connect(app.state.db_path)


CSV = (
    b"Employee ID,First Name,Last Name,Email ID,Department,Reporting To,Employee Status\n"
    b"E1,Asha,Rao,asha@acme.in,Sales,Ravi,Active\n"
    b"E2,Ravi,Kumar,RAVI@acme.in,Sales,,Active\n"
    b"E3,Old,Timer,old@acme.in,Ops,,Resigned\n"
    b"E4,No,Email,,Ops,,Active\n"
    b"E5,Dev,Patel,dev@acme.in,Engineering,,Active\n"
)


# ---------------------------------------------------------------- pure rules


def test_switch(monkeypatch):
    monkeypatch.delenv("GRC_TRAINING", raising=False)
    monkeypatch.delenv("GRC_DOMAIN", raising=False)
    assert training_views.enabled()  # a laptop / test machine
    monkeypatch.setenv("GRC_DOMAIN", "app.grc-flow.com")
    assert not training_views.enabled()  # the live server
    monkeypatch.setenv("GRC_TRAINING", "1")
    assert training_views.enabled()
    monkeypatch.delenv("GRC_DOMAIN")
    monkeypatch.setenv("GRC_TRAINING", "0")
    assert not training_views.enabled()


def test_course_is_well_formed():
    assert len(training.COURSE) >= 6
    for lesson in training.COURSE:
        assert len(lesson.quiz) >= 5 and lesson.body
        for q in lesson.quiz:
            assert 0 <= q.answer < len(q.options)


def test_score_and_pass_mark():
    lesson = training.COURSE[0]
    right = {f"q{i}": str(q.answer) for i, q in enumerate(lesson.quiz)}
    assert training.score(lesson, right) == (100, [])
    four = {**right, "q0": "99"}
    assert training.score(lesson, four)[0] == 80 and training.passed(80)
    three = {**four, "q1": "99"}
    assert training.score(lesson, three)[0] == 60 and not training.passed(60)
    assert training.passed(70)


def test_watch_clock_cannot_be_skipped():
    # Jumping to the end after 5 real seconds only counts 2x speed (plus slack).
    assert training.advance(0, 600, 5, 600) == 12
    # Normal play at 1x counts fully; at 2x too.
    assert training.advance(100, 105, 5, 600) == 105
    assert training.advance(100, 110, 5, 600) == 110
    # Going back doesn't lose anything.
    assert training.advance(300, 10, 5, 600) == 300
    assert training.watch_complete(599, 600) and not training.watch_complete(500, 600)


def test_csv_import_matches_columns():
    people, skipped = hr.from_csv(CSV)
    assert [p.email for p in people] == ["asha@acme.in", "ravi@acme.in", "dev@acme.in"]
    assert people[0].name == "Asha Rao" and people[0].department == "Sales"
    assert people[0].manager == "Ravi" and people[0].external_id == "E1"
    assert len(skipped) == 2
    with pytest.raises(base.ConnectorError):
        hr.from_csv(b"Name,Department\nA,B\n")


def test_zoho_people(monkeypatch):
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        if "oauth" in url:
            return base.Response(200, {"access_token": "tok"})
        return base.Response(
            200,
            {
                "response": {
                    "result": [
                        {
                            "1": [
                                {
                                    "EmailID": "a@x.in",
                                    "FirstName": "A",
                                    "LastName": "B",
                                    "Department": "HR",
                                    "EmployeeID": "7",
                                    "Employeestatus": "Active",
                                }
                            ]
                        },
                        {"2": [{"EmailID": "gone@x.in", "Employeestatus": "Resigned"}]},
                    ],
                    "status": 0,
                }
            },
        )

    monkeypatch.setattr(hr, "request", fake)
    people = hr.zoho_people("in", "id", "secret", "refresh")
    assert people == [hr.Person("A B", "a@x.in", "HR", "", "7")]
    assert calls[0][1] == "https://accounts.zoho.in/oauth/v2/token"
    assert calls[1][2]["headers"]["Authorization"] == "Zoho-oauthtoken tok"
    with pytest.raises(base.ConnectorError):
        hr.zoho_people("evil.com", "a", "b", "c")


def test_youtube_and_duration_parsing():
    assert (
        training_views.youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1")
        == "dQw4w9WgXcQ"
    )
    assert training_views.youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert training_views.youtube_id("https://evil.com/x") == ""
    assert training_views.parse_duration("6:30") == 390
    assert training_views.parse_duration("1:02:03") == 3723
    assert training_views.parse_duration("abc") == 0


# ---------------------------------------------------------------- the pages


def import_people(authed, eid):
    return authed.post(
        f"/engagements/{eid}/training/import",
        data={"csrf": csrf(authed, "/")},
        files={"file": ("people.csv", CSV, "text/csv")},
        follow_redirects=True,
    )


def link_for(authed, eid, emp_id) -> str:
    page = post(
        authed, f"/engagements/{eid}/training/employees/{emp_id}/link", follow_redirects=True
    ).text
    return re.search(r'value="https?://[^"]+(/learn/[^"]+)"', page).group(1)


def test_admin_page_import_and_report(app, authed):
    eid = create(authed)
    page = authed.get(f"/engagements/{eid}/training")
    assert page.status_code == 200 and "No employees yet" in page.text
    r = import_people(authed, eid)
    assert "Imported: 3 added" in r.text and "Asha Rao" in r.text
    assert "Sales" in r.text and "Skipped 2" in r.text
    # Importing again updates, never duplicates.
    assert "0 added, 3 updated" in import_people(authed, eid).text
    report = authed.get(f"/engagements/{eid}/training.csv")
    assert report.status_code == 200 and "asha@acme.in" in report.text


def test_training_hidden_on_the_live_server(app, authed, monkeypatch):
    eid = create(authed)
    monkeypatch.setenv("GRC_DOMAIN", "app.grc-flow.com")
    assert authed.get(f"/engagements/{eid}/training").status_code == 404
    assert '/training"' not in authed.get(f"/engagements/{eid}").text
    assert authed.get("/learn/anything").status_code == 404


def test_learner_flow(app, authed):
    eid = create(authed)
    import_people(authed, eid)
    with db(app) as conn:
        emp_id = conn.execute("SELECT id FROM employees WHERE email = 'asha@acme.in'").fetchone()[0]
    path = link_for(authed, eid, emp_id)
    lesson = training.COURSE[0]
    learner = app_client(app)
    home = learner.get(path)
    assert home.status_code == 200 and "Hello Asha" in home.text
    assert home.headers["cache-control"] == "no-store"
    page = learner.get(f"{path}/lesson/{lesson.id}")
    assert page.status_code == 200 and lesson.title in page.text

    # The quiz is refused before the lesson is done.
    answers = {f"q{i}": str(q.answer) for i, q in enumerate(lesson.quiz)}
    r = learner.post(f"{path}/lesson/{lesson.id}/quiz", data=answers, follow_redirects=True)
    assert "Finish the lesson first" in r.text

    # Claiming to be at the end straight away only banks a couple of seconds.
    beat = learner.post(f"{path}/lesson/{lesson.id}/beat", json={"position": 10_000}).json()
    assert beat["watched"] <= 2 and not beat["complete"]

    # Pretend the whole lesson was watched in real time.
    finish(app, emp_id, lesson.id)
    wrong = {**answers, "q0": "9", "q1": "9"}
    r = learner.post(f"{path}/lesson/{lesson.id}/quiz", data=wrong, follow_redirects=True)
    assert "60%: not passed" in r.text
    with db(app) as conn:
        row = progress(conn, emp_id, lesson.id)
    assert row["watched_seconds"] == 0 and row["attempts"] == 1  # watch it again

    finish(app, emp_id, lesson.id)
    r = learner.post(f"{path}/lesson/{lesson.id}/quiz", data=answers, follow_redirects=True)
    assert "Passed with 100%" in r.text
    with db(app) as conn:
        row = progress(conn, emp_id, lesson.id)
    assert row["passed_at"] and row["best_score"] == 100 and row["attempts"] == 2

    admin = authed.get(f"/engagements/{eid}/training").text
    assert "Leaderboard" in admin and "1/6 · 100%" in admin

    # A new link stops the old one.
    link_for(authed, eid, emp_id)
    assert learner.get(path).status_code == 404


def test_policy_acceptance(app, authed):
    eid = create(authed)
    import_people(authed, eid)
    r = post(
        authed,
        f"/engagements/{eid}/r/policies",
        {
            "title": "Privacy policy",
            "kind": "notice",
            "version": "2",
            "approved_by": "CEO",
            "approved_on": "2026-01-01",
        },
    )
    rid = int(r.url.path.rsplit("/", 1)[1])
    post(authed, f"/engagements/{eid}/r/policies/{rid}/status", {"status": "published"})
    with db(app) as conn:
        emp_id = conn.execute("SELECT id FROM employees LIMIT 1").fetchone()[0]
    path = link_for(authed, eid, emp_id)
    learner = app_client(app)
    assert "I have read and accept this" in learner.get(path).text
    r = learner.post(f"{path}/policy/{rid}", follow_redirects=True)
    assert "Accepted" in r.text
    with db(app) as conn:
        assert conn.execute("SELECT version FROM training_acks").fetchone()[0] == "2"


def test_viewers_and_outsiders(app, authed, client):
    from helpers import MEMBER_PASSWORD, add_member

    eid = create(authed)
    add_member(app, "stranger")
    other = app_client(app)
    other.post(
        "/login", data={"username": "stranger", "password": MEMBER_PASSWORD, "csrf": csrf(other)}
    )
    assert other.get(f"/engagements/{eid}/training").status_code == 404
    assert (
        post(
            other, f"/engagements/{eid}/training/employees", {"name": "X", "email": "x@x.in"}
        ).status_code
        == 404
    )


def test_video_upload_checks(app, authed):
    eid = create(authed)
    lesson = training.COURSE[0].id
    r = authed.post(
        f"/engagements/{eid}/training/media/{lesson}",
        data={"csrf": csrf(authed, "/"), "duration": "1:00"},
        files={"file": ("fake.mp4", b"not a video at all", "video/mp4")},
        follow_redirects=True,
    )
    assert "look like a video" in r.text
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
    r = authed.post(
        f"/engagements/{eid}/training/media/{lesson}",
        data={"csrf": csrf(authed, "/"), "duration": "1:00"},
        files={"file": ("intro.mp4", mp4, "video/mp4")},
        follow_redirects=True,
    )
    assert "Video set for" in r.text and "Video · 1:00" in r.text
    r = post(
        authed,
        f"/engagements/{eid}/training/media/{lesson}",
        {"youtube": "https://youtu.be/dQw4w9WgXcQ", "duration": "3:33"},
        follow_redirects=True,
    )
    assert "Video · 3:33" in r.text
    with db(app) as conn:
        assert conn.execute("SELECT kind FROM training_media").fetchone()[0] == "youtube"


# ---------------------------------------------------------------- helpers


def app_client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def progress(conn, emp_id, lesson_id):
    return conn.execute(
        "SELECT * FROM training_progress WHERE employee_id = ? AND lesson_id = ?",
        (emp_id, lesson_id),
    ).fetchone()


def finish(app, emp_id, lesson_id):
    """Mark a lesson as fully watched, as many real-time beats would."""
    lesson = training.BY_ID[lesson_id]
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with db(app) as conn:
        if progress(conn, emp_id, lesson_id) is None:
            eid = conn.execute(
                "SELECT engagement_id FROM employees WHERE id = ?", (emp_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO training_progress (employee_id, engagement_id, lesson_id) "
                "VALUES (?,?,?)",
                (emp_id, eid, lesson_id),
            )
        conn.execute(
            "UPDATE training_progress SET watched_seconds = ?, duration_seconds = ?, "
            "last_beat_at = ? WHERE employee_id = ? AND lesson_id = ?",
            (lesson.read_seconds, lesson.read_seconds, long_ago, emp_id, lesson_id),
        )


def test_training_lives_under_organisation(app, authed):
    eid = create(authed)
    # One workspace: straight to it.
    r = authed.get("/training", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/engagements/{eid}/training"
    side = authed.get("/").text
    assert 'href="/training"' in side
    # Not a tab inside the client any more.
    assert f'href="/engagements/{eid}/training"' not in authed.get(f"/engagements/{eid}").text
    # Several: an overview of each, with completion.
    eid2 = create(authed)
    import_people(authed, eid2)
    page = authed.get("/training").text
    assert f"/engagements/{eid}/training" in page and f"/engagements/{eid2}/training" in page
    assert "0 of 3 people" in page
