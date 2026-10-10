# How the code is organised

A guide to the GRC Flow app's source, for reading it or deciding where a change goes.
Every Python file also starts with a short description of its own job.

## The big picture

```
Browser ──HTTPS──► Caddy ──► FastAPI app (src/grc_agent/web/) ──► SQLite or PostgreSQL
                               │
                               ├─ assessment, risk, plan, documents  (the DPDP logic)
                               ├─ corpus + citations                 (text of the Act and Rules)
                               ├─ discovery                          (finds personal data in files)
                               ├─ connectors                         (read-only AWS, GitHub, ...)
                               └─ llm / agent / ai_assessment        (the AI, always reviewed by a person)
```

The app renders HTML on the server (Jinja templates in `web/templates/`, styles and a
little JavaScript in `web/static/`). There is no separate front-end build.

## What happens on a request

1. **Caddy** (`deploy/Caddyfile`) gets the HTTPS certificate, adds security headers and
   forwards the request to the app.
2. **Middleware in `web/app.py` and `web/https.py`**, in this order:
   - the session cookie is read (`SessionMiddleware`)
   - `end_stale_sessions` signs out sessions whose user was removed or whose password has changed
   - security headers are added, and plain HTTP is redirected to HTTPS
3. **The route** (a function decorated with `@app.get`/`@app.post`) runs:
   - `User` (a FastAPI dependency) requires a signed-in user; without one the request goes to `/login`.
   - Before that, the middleware in `create_app()` refuses staff-only pages and answers "not found" for any `/engagements/<id>` the person can't open (`web/access.py`).
   - Every form post goes through `form_with_csrf()`, which checks the CSRF token.
   - Admin-only actions call `require_admin()`; an engagement lead's calls use `access.leads()`.
4. **The database** is reached through `db.connect()`.
   - It returns SQLite or a PostgreSQL wrapper (`web/pg.py`); both take the same SQL.
   - User values always go in as `?` parameters.
5. **Every change** is written to the audit log with `db.audit()`. Each entry is chained
   to the previous one by a SHA-256 hash, so later edits to the log can be detected.

## Workspaces and delivery

- Each workspace (an "engagement" in the code) is for a client of a GRC partner or for the
  company itself (`engagements.audience`: `client` or `self`). Only the wording changes:
  rules, checks and gates are the same.
- Delivery (sign-off, for a company's own workspace) locks the intake, assessment and
  documents. The registers, risks, controls, evidence, discovery and data flow stay open,
  because breaches and requests keep their legal clocks. An admin can reopen a delivery.
- The generated documents (`documents.py`) draw on the data inventory, the vendor and DPIA
  registers, and fall back to the intake answers when those are still empty.

## Folders and files

### The DPDP logic (no web code)

| File | Job |
|---|---|
| `register.py` | The obligation register and the intake questions behind it |
| `assessment.py` | Rule-based assessment: intake answers to one finding per obligation |
| `ai_assessment.py` | AI-drafted wording for findings, on top of the rule-based result |
| `citations.py` | Checks that every cited Section or Rule exists in the corpus |
| `risk.py` | Risk register: each gap scored likelihood × impact |
| `plan.py` | Readiness plan: obligations grouped into steps, progress from live data |
| `documents.py` | Gap report, RoPA, privacy notice, breach playbook, DPA draft |
| `dataflow.py` | The personal-data flow map, including flows leaving India |
| `evidence_check.py` | Asks the AI whether an evidence file is about its obligation |
| `corpus/` | Loads the text of the Act and Rules, and searches it |
| `discovery/` | Finds personal data (Aadhaar, PAN, phone, ...) in uploaded CSV/JSON |

### AI

| File | Job |
|---|---|
| `llm.py` | Talks to any OpenAI-compatible provider (Groq, Gemini, Mistral, ...) |
| `agent.py`, `tools.py`, `prompts.py` | The GRC Analyst: the conversation loop and the read-only tools it may call |
| `demo.py` | A stand-in AI for testing without an API key |
| `web/analyst.py` | The Analyst's tools over the workspace data |
| `web/ai_views.py` | The AI provider settings page |

### Connectors (`connectors/`)

Read-only links to a client's systems. `base.py` has the shared HTTPS client. It only
calls public HTTPS addresses, and it checks every redirect. `secrets.py` encrypts stored
credentials with Fernet from the `cryptography` library.

### The web app (`web/`)

| File | Job |
|---|---|
| `app.py` | Creates the app: middleware, sign-in, engagements, findings, documents |
| `auth_views.py`, `oauth.py`, `mailer.py` | Google/Microsoft sign-in, invites, password resets, account page |
| `security.py` | Password hashing (Argon2id via argon2-cffi) and CSRF tokens |
| `https.py` | Certificates, security headers, HTTPS redirect, safe AI-provider addresses |
| `db.py`, `pg.py` | Database schema, connections, the audit log, the SQLite/PostgreSQL bridge |
| `ops_views.py` | Controls, evidence library, audit log page, work queue, team |
| `registers.py`, `register_views.py` | Tasks, consent, rights requests, breaches, erasure, drills, vendors, DPIAs, policies, systems, accepted gaps |
| `obligation_views.py` (+ `grc_agent/obligations.py`) | Full DPDP obligations register, penalty exposure, readiness trend, re-checks due |
| `training_views.py` (+ `grc_agent/training.py`, `connectors/hr.py`) | Employee training: HR import, personal links, no-skip lessons, quizzes, leaderboard |
| `board_pack.py` | Board inquiry pack ZIP and the breach Board-report draft |
| `discovery_views.py`, `dataflow_views.py`, `risk_views.py` | The pages for those features |
| `connector_views.py`, `mcp_views.py` | Connector pages; API keys and the MCP server for AI apps |
| `notify.py`, `notification_views.py` | In-app notifications |
| `datamanager.py` | What the workspace stores, how much, and what's past its retention |
| `demo_tenant.py` | A sample workspace with made-up clients, for demos |
| `cli.py` | The `grc-web` command: run the app, add users, migrate to PostgreSQL |

## Security, in one place

| Protection | Where |
|---|---|
| Passwords: Argon2id, old hashes upgraded at sign-in | `web/security.py`, `login()` in `web/app.py` |
| Same response for unknown users; lockout per user and per address | `login()` in `web/app.py` |
| Sessions end when a user is removed or changes password | `end_stale_sessions` in `web/app.py` |
| CSRF on every form | `form_with_csrf()` in `web/app.py` |
| Each organisation sees only its own engagements; viewers are read-only | `web/access.py`, `end_stale_sessions` and `form_with_csrf()` in `web/app.py` |
| No requests to private or cloud-metadata addresses | `connectors/base.py`, `insecure_url_problem()` in `web/https.py` |
| Uploads: type checked against content, size limit, random names | `web/ops_views.py` |
| Security headers, HTTPS only | `web/https.py`, `deploy/Caddyfile` |
| Encrypted connector and AI keys | `connectors/secrets.py` |

Security scanners run in CI on every pull request:

- `ruff check .` includes the Bandit security rules (`S`).
- `pip-audit` checks every dependency against published vulnerabilities.

Where a scanner flags something that is safe by design, the line says so: a short
comment explains why, then `# nosec <Bandit id>  # noqa: <ruff id>`.

The latest full review is `docs/SECURITY_REVIEW_2026-10.md`.

## Where to start for common changes

- **A new intake question or obligation:** `register.py` and its data file, then `assessment.py`.
- **A new page:** add a route in the matching `web/*_views.py` and a template in `web/templates/`.
- **A new database column:** the schema in `web/db.py`. Migrations run when the app starts.
- **A new connector:** a module in `connectors/` using `base.request()`, listed in `connectors/catalog.py`.
- **A new AI provider:** the `PROVIDERS` table in `llm.py`.
