# System design

How a request moves through the GRC agent, from the user's click to the
feedback they see. GitHub renders the diagrams below.

## 1. Architecture

```mermaid
flowchart LR
  U([Consultant's browser]) -->|HTTPS form posts and page loads| MW
  subgraph Server["FastAPI app (grc-web serve)"]
    MW[Session cookie + CSRF check] --> R[Route handler]
    R --> T[Jinja2 template]
    R --> AU[db.audit]
    AU --> NT[notify.from_audit]
    R --> BG[BackgroundTasks]
    R --> AG[GRC Analyst agent loop]
    R --> AS[Assessment engine<br/>rules or Claude]
    R --> DF[Data-flow builder]
    R --> RK[Risk register builder]
    R --> DM[Data manager<br/>read-only]
  end
  R <--> DB[(SQLite grc.db)]
  AU --> DB
  NT --> DB
  DM --> DB
  AG <-->|tool loop| CL[Claude API]
  AS <--> CL
  AG -->|read-only tools| DB
  BG -->|updates| CH[Slack / Teams / Google Chat]
  R -->|read-only checks| EV[Client GitHub / AWS]
  T -->|HTML + flash message| U
  U -.->|every 20 s| NJ["GET /notifications/unread.json"]
  U -.->|every 10 s| FJ["GET /engagements/id/dataflow.json"]
```

**The same six steps for every request:**

1. **Call:** the browser sends a GET (page) or POST (form) request.
2. **Gate:** the session cookie names the user. With no user, the request is redirected to `/login` (303). POSTs must also carry the CSRF token, otherwise 403.
3. **Work:** the route reads or writes SQLite over one connection. The connection commits when the request ends.
4. **Record:** state changes call `db.audit()`. That writes the audit log and, through `notify.from_audit()`, a notification.
5. **Side effects:** chat messages go out as `BackgroundTasks` after the response. Claude calls run in a thread pool.
6. **Feedback:** POSTs end with a **303 redirect** and a **flash message**, which is shown on the next page. GETs render HTML. Other users see a **toast** on their next 20-second poll.

## 2. Login

```mermaid
sequenceDiagram
  actor U as User
  participant B as Browser
  participant S as FastAPI
  participant D as SQLite
  U->>B: open /login
  B->>S: GET /login
  S-->>B: login page + CSRF token (new session)
  U->>B: username, password, Sign in
  Note over B: app.js: Caps Lock hint, show/hide password,<br/>button turns to "Signing in…"
  B->>S: POST /login (csrf)
  S->>S: CSRF ok? lockout (5 fails / 5 min)?
  S->>D: SELECT user, verify password hash
  alt wrong password
    S->>D: audit login_failed → security notification for everyone
    S-->>B: 401 login page, username kept, error shakes
  else locked out
    S-->>B: 429 "Too many failed attempts"
  else ok
    S->>S: clear session, set user + new CSRF
    S->>D: audit login
    S-->>B: 303 → /
    B->>S: GET / (dashboard)
  end
  Note over U,B: Planned buttons (Google/Microsoft SSO, Forgot password,<br/>Keep me signed in, Request access) show a "coming soon" toast
```

## 3. Dashboard load

```mermaid
sequenceDiagram
  participant B as Browser
  participant S as GET /
  participant D as SQLite
  B->>S: GET / (session cookie)
  S->>D: engagements → per-engagement summary (stage, score, gaps)
  S->>D: audit_log (last 8), connections
  S->>S: pipeline, needs-attention, risk queue (analyst.queue)
  S->>S: data flows → how many clients send data out of India
  S->>D: data manager report (no integrity check)
  S-->>B: dashboard.html
  Note over B: app.js: greeting by local time, "Getting started" hide (localStorage),<br/>Ctrl K search, g+key shortcuts
  loop every 20 s
    B->>S: GET /notifications/unread.json?after=N
    S-->>B: unread count + new items → bell badge + toasts
  end
```

## 4. Engagement workflow: create, intake, assess, review, deliver

```mermaid
sequenceDiagram
  actor U as Consultant
  participant S as FastAPI
  participant D as SQLite
  participant C as Claude API
  participant X as Chat connectors
  U->>S: POST /engagements (client, sector)
  S->>D: INSERT engagement, audit engagement_created
  S-->>U: 303 → /engagements/{id}
  U->>S: POST /engagements/{id}/intake (answers, save|submit)
  S->>D: upsert intake_answers, set intake_submitted_at
  S->>D: if already assessed and answers changed → stale = 1 (warning)
  S-->>X: background: "intake submitted"
  S-->>U: 303 + flash "Intake submitted."
  U->>S: POST /engagements/{id}/assess (mode = rules | claude)
  alt Claude
    S->>C: assessor.assess(register, answers) in thread pool
    C-->>S: findings with citations (or error → flash, nothing changed)
  else rules
    S->>S: assess(register, answers, corpus index)
  end
  S->>D: replace findings, clear documents, assessed_at, audit
  S-->>X: background: "assessment run, N findings"
  S-->>U: 303 → findings + flash (complete / citations unresolved)
  U->>S: POST /engagements/{id}/findings/{fid} (verdict)
  S->>D: UPDATE finding verdict, audit
  U->>S: POST /engagements/{id}/documents/generate
  S->>D: build notice, policy and report from findings, audit
  S-->>X: background: "draft pack ready"
  U->>S: POST .../documents/{did}/review (outcome)
  U->>S: POST /engagements/{id}/deliver
  S->>S: delivery_checks (all reviewed, citations resolve, not stale…)
  alt a check fails
    S-->>U: 303 + flash "Can't deliver yet: …"
  else all pass
    S->>D: delivered_at, audit delivered
    S-->>X: background: "delivered"
    S-->>U: 303 + flash "Engagement marked as delivered."
  end
```

## 5. GRC Analyst question

```mermaid
sequenceDiagram
  actor U as User
  participant S as POST /assistant
  participant A as Agent (per user)
  participant C as Claude API
  participant T as Analyst tools
  participant D as SQLite
  U->>S: question (+ focus client from session)
  S->>S: prefix "[Focus: ENG-001 Acme]" if a client is selected
  S->>A: agent.ask(question) in thread pool
  loop until Claude stops asking for tools
    A->>C: messages.create(system = ANALYST_PROMPT, tools, history)
    C-->>A: tool_use (e.g. get_risk_register)
    A->>T: run_tool(name, input), strict schema
    T->>D: read-only query (own connection)
    T-->>A: JSON result (or ToolError)
  end
  C-->>A: final answer text
  A-->>S: AgentResult(text, tool_calls)
  S-->>U: 303 → /assistant + flash "Tools used: …"
  Note over U: GET /assistant renders the chat with Markdown escaped,<br/>follow-up chips, analyst queue
```

## 6. Evidence connectors

```mermaid
sequenceDiagram
  actor U as User
  participant S as FastAPI
  participant K as SecretBox
  participant E as Client GitHub / AWS
  participant D as SQLite
  U->>S: POST /engagements/{id}/connectors (type, fields)
  S->>E: test connection (read-only) in thread pool
  alt fails
    S-->>U: 303 back + flash "Couldn't connect to …"
  else ok
    S->>K: encrypt secrets
    S->>D: INSERT connection, audit
    S-->>U: 303 → connectors
  end
  U->>S: POST .../connectors/{cid}/sync
  S->>E: run read-only checks (MFA, logging, branch protection…)
  S->>D: replace evidence rows, last_synced_at, audit
  S-->>U: 303 + flash, and failed checks appear in the risk register and data-flow map
```

## 7. Notifications

```mermaid
sequenceDiagram
  participant R as Any route
  participant A as db.audit
  participant N as notify.from_audit
  participant D as SQLite
  participant B as Other users' browsers
  R->>A: audit(user, action, engagement, detail)
  A->>D: INSERT audit_log
  A->>N: map action → (category, level, title, link) or skip
  N->>D: INSERT notification, mark read for the actor
  loop every 20 s
    B->>D: GET /notifications/unread.json?after=latest
    D-->>B: new items → toast + bell count + tab title "(3)"
  end
  B->>D: GET /notifications/{id}/open → mark read, 303 to the link
```

## 8. Live data-flow map

```mermaid
sequenceDiagram
  participant B as Browser (dataflow.js)
  participant S as FastAPI
  participant F as dataflow.build
  participant D as SQLite
  B->>S: GET /engagements/{id}/dataflow
  S-->>B: page + initial JSON (tojson)
  loop every 10 s
    B->>S: GET /engagements/{id}/dataflow.json
    S->>D: intake answers, findings, evidence, custom nodes
    S->>F: nodes, edges, issues, mitigation plan
    F-->>B: JSON → SVG redraw, animated dots, issue markers
  end
  B->>S: POST .../dataflow/nodes (add a system) → audit → notification
  B->>S: GET .../dataflow/plan.csv (formula-safe export)
```

## 9. Risk register edit

```mermaid
sequenceDiagram
  actor U as User
  participant S as POST /engagements/{id}/risks
  participant D as SQLite
  U->>S: risk_key, likelihood, impact, treatment, owner, due, status, notes
  S->>S: validate: known key (404), valid treatment/status (400),<br/>due is a date, accept needs a reason
  S->>D: UPSERT risk_edits, audit risk_updated
  Note over D: accept → warning notification "risk accepted"
  S-->>U: 303 → risks + flash "Saved: …"
```

## 10. Data manager

```mermaid
sequenceDiagram
  actor U as User
  participant S as GET /data-manager
  participant M as datamanager.report
  participant D as SQLite
  U->>S: open Data manager
  S->>M: report(conn, db_path)
  M->>D: sqlite_master tables, COUNT(*), MIN/MAX dates per table
  M->>D: PRAGMA page_count, freelist_count, quick_check, journal_mode
  M->>M: match each table to the catalogue (purpose, personal data, retention)
  M->>M: watch list: uncatalogued tables, rows past retention, free space, no backups
  M-->>S: report
  S-->>U: tiles, watch list, files, filterable catalogue
  Note over U: Back up, Export, Purge, Set retention and Compact<br/>are planned buttons ("coming soon" toast)
```

## 11. Planned buttons

```mermaid
flowchart LR
  C["Click a button with data-soon=key"] --> J[app.js reads the planned-features JSON]
  J --> T["Toast: X is coming soon + what the backend will do"]
  P["planned.py PLANNED registry"] -->|rendered into every page| J
  P -.->|tests/test_ui.py| CHK{every data-soon key registered?}
```

To build one: add the route, replace the `data-soon` button with a real form or link, delete the entry from `planned.py`, and add a test.
