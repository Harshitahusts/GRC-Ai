"""A separate demo workspace, filled with sample clients, for showing the product.

`grc-web demo` creates it in its own data folder (default ./var-demo) and serves it on
its own port, so the main workspace in ./var is never touched. A `DEMO_TENANT` marker
file in the folder switches on demo behaviour:

- a banner on every page and the demo login on the sign-in page;
- connector syncs are simulated (no calls to GitHub or AWS);
- a "live" loop makes a colleague act every so often (a connector re-sync, a finding
  reviewed, a risk picked up), so notifications, toasts, the risk register and the
  data-flow map change while you present.

Everything is seeded through the app's own routes, so the data is shaped exactly like
real data: the same findings engine, documents, audit log and notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from grc_agent.connectors.base import SECURITY, Check, location_check
from grc_agent.web import db
from grc_agent.web.security import hash_password

log = logging.getLogger(__name__)

MARKER = "DEMO_TENANT"
DEMO_USER = "demo"
DEMO_PASSWORD = "grc-demo-2026"  # sample data only; the demo runs on this machine
COLLEAGUES = ("priya.sharma", "arjun.mehta")
LIVE_SECONDS = 40  # how often a colleague does something in the live demo

QUESTIONS = [
    "Q-NOTICE",
    "Q-CONSENT",
    "Q-WITHDRAW",
    "Q-SECURITY",
    "Q-BREACH",
    "Q-ERASURE",
    "Q-CONTACT",
    "Q-GRIEVANCE",
    "Q-ACCESS",
    "Q-CORRECTION",
    "Q-VENDOR-CONTRACT",
]


def is_demo(data_dir: Path) -> bool:
    return (Path(data_dir) / MARKER).exists()


# ---------------------------------------------------------------- sample clients

# stage: how far through the workflow the client is.
# no / unsure: intake questions answered No or Not sure (everything else Yes).
CLIENTS = [
    {
        "client": "Pinecrest Learning Pvt Ltd",
        "sector": "EdTech",
        "stage": "delivered",
        "days_ago": 24,
        "info": {
            "INFO-DATA": "Student names, dates of birth, parent phone numbers and emails, "
            "test scores",
            "INFO-PURPOSE": "Running live classes, sending progress reports to parents",
            "INFO-TOOLS": "Zoho Desk, Razorpay, Tally",
            "INFO-RETENTION": "Two years after the student leaves, then deleted",
            "INFO-CONTACT": "Meera Iyer, Data Protection Officer, dpo@pinecrest.example",
        },
        "ctx": {"CTX-CHILDREN": "yes", "CTX-VENDORS": "yes", "CTX-FOREIGN": "no"},
        "extra": {"Q-PARENTAL": "yes"},
        "no": [],
        "unsure": [],
        "aws": {"ap-south-1": ["pinecrest-classes", "pinecrest-reports"]},
        "aws_fail": [],
        "github": None,
        "nodes": [("Parent WhatsApp updates", "destinations", "india", "Phone numbers")],
    },
    {
        "client": "Kaveri Finserv Ltd",
        "sector": "BFSI",
        "stage": "ready",
        "days_ago": 18,
        "info": {
            "INFO-DATA": "Names, PAN, Aadhaar last 4 digits, bank account numbers, "
            "income, credit scores",
            "INFO-PURPOSE": "Loan underwriting, KYC, collections",
            "INFO-TOOLS": "AWS, Salesforce, Mailchimp, CIBIL",
            "INFO-RETENTION": "Eight years after the loan closes (RBI rules)",
            "INFO-CONTACT": "Rohit Menon, Grievance Officer, grievance@kaveri.example",
        },
        "ctx": {"CTX-CHILDREN": "no", "CTX-VENDORS": "yes", "CTX-FOREIGN": "yes"},
        "extra": {"Q-TRANSFER": "yes"},
        "no": [],
        "unsure": ["Q-ERASURE"],
        "aws": {"ap-south-1": ["kaveri-kyc", "kaveri-loans"], "us-east-1": ["kaveri-analytics"]},
        "aws_fail": [],
        "github": {"account": "kaveri-finserv", "fail": ["branch_protection"]},
        "nodes": [("CIBIL bureau", "vendors", "india", "PAN, credit history")],
    },
    {
        "client": "Arogya Health Clinics",
        "sector": "Healthcare",
        "stage": "review",
        "days_ago": 12,
        "info": {
            "INFO-DATA": "Patient names, phone numbers, prescriptions, lab reports, insurance IDs",
            "INFO-PURPOSE": "Appointments, treatment records, insurance claims",
            "INFO-TOOLS": "Practo, AWS, Google Workspace, WhatsApp Business",
            "INFO-RETENTION": "Not defined yet",
            "INFO-CONTACT": "Dr. Kavya Rao, Medical Director",
        },
        "ctx": {"CTX-CHILDREN": "yes", "CTX-VENDORS": "yes", "CTX-FOREIGN": "yes"},
        "extra": {"Q-PARENTAL": "not_sure", "Q-TRANSFER": "no"},
        "no": ["Q-SECURITY", "Q-ERASURE"],
        "unsure": ["Q-BREACH"],
        "aws": {"ap-south-1": ["arogya-records"], "eu-west-1": ["arogya-backups"]},
        "aws_fail": ["root_mfa", "password_policy"],
        "github": None,
        "nodes": [
            ("Diagnostic lab portal", "vendors", "india", "Lab reports"),
            ("Insurance TPA", "destinations", "india", "Claims, insurance IDs"),
        ],
    },
    {
        "client": "Bazaarkart Retail Pvt Ltd",
        "sector": "Retail",
        "stage": "assessed",
        "days_ago": 8,
        "info": {
            "INFO-DATA": "Customer names, addresses, phone numbers, order history, UPI IDs",
            "INFO-PURPOSE": "Orders, delivery, marketing offers",
            "INFO-TOOLS": "Shopify, Mailchimp, Razorpay, Freshdesk",
            "INFO-RETENTION": "Kept indefinitely for marketing",
            "INFO-CONTACT": "support@bazaarkart.example",
        },
        "ctx": {"CTX-CHILDREN": "no", "CTX-VENDORS": "yes", "CTX-FOREIGN": "yes"},
        "extra": {"Q-TRANSFER": "not_sure"},
        "no": ["Q-CONSENT", "Q-WITHDRAW", "Q-VENDOR-CONTRACT"],
        "unsure": ["Q-ERASURE"],
        "aws": None,
        "aws_fail": [],
        "github": {"account": "bazaarkart", "fail": ["branch_protection", "secret_scanning"]},
        "nodes": [
            ("Shopify storefront", "collection", "outside", "Orders, addresses"),
            ("Delhivery courier", "vendors", "india", "Names, addresses, phone numbers"),
        ],
    },
    {
        "client": "Nimbus HR Cloud",
        "sector": "SaaS",
        "stage": "submitted",
        "days_ago": 4,
        "info": {
            "INFO-DATA": "Employee names, salaries, bank details, attendance, appraisals",
            "INFO-PURPOSE": "Payroll and HR for client companies",
            "INFO-TOOLS": "AWS, Keka, Slack",
            "INFO-RETENTION": "As long as the client contract lasts",
            "INFO-CONTACT": "privacy@nimbushr.example",
        },
        "ctx": {"CTX-CHILDREN": "no", "CTX-VENDORS": "yes", "CTX-FOREIGN": "yes"},
        "extra": {"Q-TRANSFER": "yes"},
        "no": ["Q-GRIEVANCE"],
        "unsure": ["Q-CORRECTION"],
        "aws": None,
        "aws_fail": [],
        "github": {"account": "nimbus-hr", "fail": []},
        "nodes": [],
    },
    {
        "client": "Sahyadri Logistics",
        "sector": "Other",
        "stage": "intake",
        "days_ago": 1,
        "info": {
            "INFO-DATA": "Driver licences, consignee names and addresses, phone numbers",
            "INFO-TOOLS": "Zoho CRM, Tally",
        },
        "ctx": {"CTX-VENDORS": "yes"},
        "extra": {},
        "no": [],
        "unsure": [],
        "partial": True,
        "aws": None,
        "aws_fail": [],
        "github": None,
        "nodes": [],
    },
]

ORDER = ["intake", "submitted", "assessed", "review", "ready", "delivered"]


def _answers(c: dict) -> dict[str, str]:
    a = {**c["info"], **c["ctx"]}
    questions = QUESTIONS[:5] if c.get("partial") else QUESTIONS
    for q in questions:
        a[q] = "no" if q in c["no"] else "not_sure" if q in c["unsure"] else "yes"
    a.update(c["extra"])
    return {f"q_{k}": v for k, v in a.items()}


# ---------------------------------------------------------------- evidence


def aws_checks(locations: dict[str, list[str]], failing: list[str]) -> list[Check]:
    def pick(key, title, ok, bad):
        return Check(
            key,
            title,
            "fail" if key in failing else "pass",
            bad if key in failing else ok,
            SECURITY,
        )

    return [
        pick("root_mfa", "MFA on the root account", "Enabled.", "The root account has no MFA."),
        pick(
            "password_policy",
            "Password policy",
            "An account password policy is set.",
            "No account password policy.",
        ),
        pick(
            "audit_logging",
            "Audit logging (CloudTrail)",
            "A multi-region trail is logging.",
            "No CloudTrail trail is logging.",
        ),
        Check(
            "public_access",
            "S3 public access block",
            "pass",
            "Blocked for the whole account.",
            SECURITY,
        ),
        location_check("AWS S3", locations, lambda r: r in {"ap-south-1", "ap-south-2"}),
    ]


def github_checks(account: str, failing: list[str]) -> list[Check]:
    repos = [f"{account}/web", f"{account}/api", f"{account}/infra"]
    return [
        Check("repositories", "Repositories", "info", f"{len(repos)} repositories checked."),
        Check("public_repositories", "Public repositories", "pass", "No public repositories."),
        Check(
            "branch_protection",
            "Default branch protection",
            "fail" if "branch_protection" in failing else "pass",
            f"Unprotected: {repos[1]}, {repos[2]}"
            if "branch_protection" in failing
            else "Protected in every repository checked.",
            SECURITY,
        ),
        Check(
            "secret_scanning",
            "Secret scanning",
            "warn" if "secret_scanning" in failing else "pass",
            f"Off in: {repos[0]}"
            if "secret_scanning" in failing
            else "On in every repository checked.",
            SECURITY,
        ),
    ]


# Details for flipping a check during a simulated re-sync.
FLIP = {
    "root_mfa": ("The root account has no MFA.", "Enabled."),
    "password_policy": ("No account password policy.", "An account password policy is set."),
    "audit_logging": ("No CloudTrail trail is logging.", "A multi-region trail is logging."),
    "public_access": (
        "Not set at account level: buckets could be made public.",
        "Blocked for the whole account.",
    ),
    "branch_protection": ("Unprotected: main in 1 repository.", "Protected in every repository."),
    "secret_scanning": ("Off in 1 repository.", "On in every repository checked."),
    "public_repositories": ("1 public repository.", "No public repositories."),
}


def _add_connection(conn, app, eid: int, connector: str, config: dict, checks: list[Check], user):
    cur = conn.execute(
        "INSERT INTO connections (engagement_id, connector, config_json, secrets_enc, "
        "secret_hints_json, message, created_by, created_at, last_synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            eid,
            connector,
            json.dumps(config),
            app.state.secret_box.seal({}),
            "{}",
            f"{len(checks)} checks collected (demo data).",
            user,
            db.now(),
            db.now(),
        ),
    )
    conn.executemany(
        "INSERT INTO evidence (connection_id, engagement_id, check_key, title, status, detail, "
        "provisions_json, data_json, collected_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                cur.lastrowid,
                eid,
                ch.key,
                ch.title,
                ch.status,
                ch.detail,
                json.dumps(ch.provisions),
                json.dumps(ch.data),
                db.now(),
            )
            for ch in checks
        ],
    )
    db.audit(conn, user, "connector_added", eid, {"connector": connector})
    db.audit(conn, user, "connector_synced", eid, {"connector": connector, "status": "ok"})


def resync(conn, row, rng: random.Random | None = None) -> str:
    """A simulated connector sync: refresh the evidence, sometimes flipping one check."""
    rng = rng or random.Random()
    rows = conn.execute(
        "SELECT id, check_key, status FROM evidence WHERE connection_id = ? "
        f"AND check_key IN ({','.join('?' * len(FLIP))})",
        (row["id"], *FLIP),
    ).fetchall()
    note = "no changes"
    if rows and rng.random() < 0.6:
        e = rng.choice(rows)
        bad, good = FLIP[e["check_key"]]
        fixed = e["status"] in ("fail", "warn")
        status = "pass" if fixed else ("warn" if e["check_key"] == "public_access" else "fail")
        conn.execute(
            "UPDATE evidence SET status = ?, detail = ? WHERE id = ?",
            (status, good if fixed else bad, e["id"]),
        )
        note = f"{e['check_key'].replace('_', ' ')} now {'passing' if fixed else 'failing'}"
    conn.execute(
        "UPDATE evidence SET collected_at = ? WHERE connection_id = ?", (db.now(), row["id"])
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE connection_id = ?", (row["id"],)
    ).fetchone()[0]
    return f"{count} checks collected (demo data): {note}."


# ---------------------------------------------------------------- seeding


class _Clock:
    """Stands in for db.now() while seeding, so the history spreads over weeks."""

    def __init__(self, start: datetime):
        self.t = start

    def at(self, t: datetime) -> None:
        self.t = max(self.t, t)

    def tick(self, minutes: float = 7) -> None:
        self.t += timedelta(minutes=minutes)

    def __call__(self) -> str:
        return self.t.isoformat(timespec="seconds")


def seed(data_dir: Path, reset: bool = False) -> Path:
    """Create the demo workspace in `data_dir`. Refuses to touch a non-demo folder."""
    from fastapi.testclient import TestClient

    from grc_agent.web.app import create_app

    data_dir = Path(data_dir)
    if data_dir.exists() and any(data_dir.iterdir()):
        if not is_demo(data_dir):
            raise SystemExit(
                f"{data_dir} holds a real workspace (no {MARKER} marker). "
                "Pick another folder for the demo."
            )
        if not reset:
            return data_dir
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / MARKER).write_text(
        json.dumps({"created_at": db.now(), "note": "Sample data for demos. Not real clients."})
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = _Clock(now - timedelta(days=30))
    real_now = db.now
    db.now = clock
    try:
        app = create_app(data_dir)
        passwords = {DEMO_USER: DEMO_PASSWORD}
        passwords.update({u: secrets.token_urlsafe(12) for u in COLLEAGUES})
        with db.connect(app.state.db_path) as conn:
            for user, pw in passwords.items():
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                    (user, hash_password(pw), db.now()),
                )
        clients = {u: _login(TestClient(app), u, pw) for u, pw in passwords.items()}
        for i, c in enumerate(CLIENTS):
            clock.at(now - timedelta(days=c["days_ago"], hours=3))
            _seed_client(app, clients, clock, c, i)
        clock.at(now - timedelta(minutes=30))
    finally:
        db.now = real_now
    return data_dir


def _login(client, user: str, password: str):
    token = _csrf(client, "/login")
    r = client.post("/login", data={"username": user, "password": password, "csrf": token})
    assert r.url.path == "/", f"demo login failed for {user}"
    return client


def _csrf(client, path: str = "/") -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def _post(client, path: str, data: dict | None = None):
    r = client.post(path, data={**(data or {}), "csrf": _csrf(client)})
    assert r.status_code < 400, f"{path}: {r.status_code}"
    return r


def _seed_client(app, clients: dict, clock: _Clock, c: dict, i: int) -> None:
    lead = clients[COLLEAGUES[i % 2]]
    lead_name = COLLEAGUES[i % 2]
    other = clients[COLLEAGUES[(i + 1) % 2]]
    me = clients[DEMO_USER]
    stage = ORDER.index(c["stage"])

    r = _post(lead, "/engagements", {"client": c["client"], "sector": c["sector"], "mode": "agent"})
    eid = int(r.url.path.rsplit("/", 1)[1])
    clock.tick(40)
    answers = _answers(c)
    _post(lead, f"/engagements/{eid}/intake", {**answers, "action": "save"})
    clock.tick(60 * 20)
    if stage >= ORDER.index("submitted"):
        _post(lead, f"/engagements/{eid}/intake", {**answers, "action": "submit"})
        clock.tick(90)

    with db.connect(app.state.db_path) as conn:
        if c["aws"]:
            _add_connection(
                conn,
                app,
                eid,
                "aws",
                {
                    "role_arn": f"arn:aws:iam::{400000000000 + i * 1111}:role/GRC-ReadOnly",
                    "region": "ap-south-1",
                    "external_id": f"demo-{eid}",
                },
                aws_checks(c["aws"], c["aws_fail"]),
                lead_name,
            )
            clock.tick(15)
        if c["github"]:
            g = c["github"]
            _add_connection(
                conn,
                app,
                eid,
                "github",
                {
                    "installation_id": str(50000000 + i),
                    "account": g["account"],
                    "repository_selection": "selected",
                },
                github_checks(g["account"], g["fail"]),
                lead_name,
            )
            clock.tick(15)
        for name, where, location, cats in c["nodes"]:
            conn.execute(
                "INSERT INTO dataflow_nodes (engagement_id, name, stage, location, categories, "
                "source, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (eid, name, where, location, cats, "", lead_name, db.now()),
            )
            db.audit(conn, lead_name, "dataflow_node_added", eid, {"name": name})
            clock.tick(10)

    if stage < ORDER.index("assessed"):
        return
    clock.tick(60 * 26)
    _post(lead, f"/engagements/{eid}/assess", {"mode": "rules"})
    clock.tick(45)

    with db.connect(app.state.db_path) as conn:
        findings = conn.execute(
            "SELECT id, status FROM findings WHERE engagement_id = ? ORDER BY id", (eid,)
        ).fetchall()
    share = 1.0 if stage >= ORDER.index("review") else 0.5
    for f in findings[: int(len(findings) * share)]:
        verdict = "incomplete" if f["status"] == "open_item" and f["id"] % 3 == 0 else "correct"
        _post(other, f"/engagements/{eid}/findings/{f['id']}", {"verdict": verdict})
        clock.tick(4)

    risks = f"/engagements/{eid}/risks"
    page = me.get(risks).text
    keys = re.findall(r'name="risk_key" value="([^"]+)"', page)
    if keys:
        clock.tick(60 * 3)
        _post(me, risks, _risk(keys[0], "mitigate", "in_progress", "CTO", clock, 14))
    if len(keys) > 2 and c["stage"] == "assessed":
        _post(
            other,
            risks,
            _risk(
                keys[2],
                "accept",
                "open",
                "Head of Marketing",
                clock,
                30,
                "Board accepted until the Shopify migration in Q1.",
            ),
        )

    if stage < ORDER.index("review"):
        return
    clock.tick(60 * 30)
    _post(lead, f"/engagements/{eid}/documents/generate")
    clock.tick(30)
    with db.connect(app.state.db_path) as conn:
        docs = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM documents WHERE engagement_id = ? ORDER BY id", (eid,)
            )
        ]
    to_review = docs if stage >= ORDER.index("ready") else docs[:1]
    for n, did in enumerate(to_review):
        clock.tick(60 * 5)
        outcome = "usable" if n % 2 == 0 else "minor_edits"
        _post(
            me, f"/engagements/{eid}/documents/{did}/review", {"outcome": outcome, "confirm": "on"}
        )

    if stage < ORDER.index("delivered"):
        return
    clock.tick(60 * 24)
    _post(me, f"/engagements/{eid}/deliver")


def _risk(key, treatment, status, owner, clock: _Clock, due_in_days: int, notes: str = "") -> dict:
    due = (clock.t + timedelta(days=due_in_days)).date().isoformat()
    return {
        "risk_key": key,
        "treatment": treatment,
        "status": status,
        "owner": owner,
        "due": due,
        "notes": notes,
    }


# ---------------------------------------------------------------- live activity


def live_step(db_path: Path, rng: random.Random | None = None) -> str | None:
    """One thing a colleague does. Returns a short description, or None if nothing to do."""
    rng = rng or random.Random()
    user = rng.choice(COLLEAGUES)
    conn = db.connect(db_path)
    try:
        with conn:  # commits when the block ends
            return _live_action(conn, user, rng)
    finally:
        conn.close()


def _live_action(conn, user: str, rng: random.Random) -> str | None:
    options = ["sync", "sync", "finding", "risk"]
    rng.shuffle(options)
    for kind in options:
        if kind == "sync":
            row = conn.execute(
                "SELECT c.* FROM connections c JOIN engagements e ON e.id = c.engagement_id "
                "WHERE e.delivered_at IS NULL ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            if row is None:
                continue
            message = resync(conn, row, rng)
            conn.execute(
                "UPDATE connections SET status = 'ok', message = ?, last_synced_at = ? "
                "WHERE id = ?",
                (message, db.now(), row["id"]),
            )
            db.audit(
                conn,
                user,
                "connector_synced",
                row["engagement_id"],
                {"connector": row["connector"], "status": "ok"},
            )
            return f"{user} re-synced {row['connector']}: {message}"
        if kind == "finding":
            f = conn.execute(
                "SELECT f.id, f.engagement_id FROM findings f JOIN engagements e "
                "ON e.id = f.engagement_id WHERE f.verdict IS NULL AND e.delivered_at IS NULL "
                "ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            if f is None:
                continue
            conn.execute("UPDATE findings SET verdict = 'correct' WHERE id = ?", (f["id"],))
            db.audit(
                conn,
                user,
                "finding_scored",
                f["engagement_id"],
                {"finding": f["id"], "verdict": "correct", "hallucination": 0},
            )
            return f"{user} reviewed finding {f['id']}"
        if kind == "risk":
            e = conn.execute(
                "SELECT e.id FROM engagements e WHERE e.assessed_at IS NOT NULL "
                "AND e.delivered_at IS NULL ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            if e is None:
                continue
            f = conn.execute(
                "SELECT obligation_id FROM findings WHERE engagement_id = ? "
                "AND status IN ('gap', 'open_item') ORDER BY RANDOM() LIMIT 1",
                (e["id"],),
            ).fetchone()
            if f is None:
                continue
            key = f"finding:{f['obligation_id']}"
            title = _obligation_title(f["obligation_id"])
            due = (datetime.now(timezone.utc) + timedelta(days=21)).date().isoformat()
            conn.execute(
                "INSERT INTO risk_edits (engagement_id, risk_key, treatment, owner, due, "
                "status, notes, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (engagement_id, risk_key) DO UPDATE SET status = 'in_progress', "
                "owner = excluded.owner, updated_by = excluded.updated_by, "
                "updated_at = excluded.updated_at",
                (e["id"], key, "mitigate", "IT lead", due, "in_progress", "", user, db.now()),
            )
            db.audit(
                conn,
                user,
                "risk_updated",
                e["id"],
                {"risk": key, "title": title, "status": "in_progress", "treatment": "mitigate"},
            )
            return f"{user} picked up {title}"
    return None


def _obligation_title(obligation_id: str) -> str:
    from grc_agent.register import load_register

    for o in load_register().obligations:
        if o.id == obligation_id:
            return o.obligation
    return obligation_id


async def live_loop(db_path: Path, seconds: float) -> None:
    rng = random.Random()
    while True:
        await asyncio.sleep(seconds * rng.uniform(0.7, 1.3))
        try:
            done = await asyncio.to_thread(live_step, db_path, rng)
            if done:
                log.info("demo live: %s", done)
        except Exception:  # the demo must never crash the server
            log.exception("demo live step failed")
