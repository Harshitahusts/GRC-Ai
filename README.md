# GRC Flow

**[grc-flow.com](https://grc-flow.com)** · A GRC workspace for **India's Digital Personal
Data Protection Act, 2023 and the DPDP Rules, 2025**. The AI parts run on Claude or on a
free OpenAI-compatible provider (Groq, Gemini, ...). It covers the DPDP Act only for now; other
frameworks come later.

The GRC Analyst chats with you and, when it needs facts, calls tools: the DPDP
obligations register, the text of the Act and Rules, 5x5 risk scoring, and (in the web
app) read-only views of the workspace's engagements, findings, risks, evidence and data
flows.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env             # then put your ANTHROPIC_API_KEY in .env
```

Get an API key at <https://console.anthropic.com>, or run `ant auth login` instead of
setting a key.

## Web app (local)

A local web app for running DPDP engagements end to end. You log in and work from a
dashboard. Each engagement follows these steps:

1. **Intake:** business-language questions, with follow-ups that appear only when relevant.
2. **Findings:** a rule-based assessment gives one finding per obligation, and each
   finding's citation is checked against the corpus. The findings page is also where the
   consultant scores accuracy.
3. **Documents:** a gap report, RoPA, privacy notice, breach playbook and a DPA marked
   draft-for-lawyer. A person has to review each one before it can be downloaded as .docx.
4. **Delivery:** blocked until every check passes (the plan's "hard stop"), with no override.

The dashboard shows what needs attention, the pipeline, top risks across clients, and
recent activity.

### Risk register

Each engagement has a **Risks** step: every gap or open item, and every failed connector
check, becomes a risk with a threat (what could happen), a vulnerability (what's
missing) and a score of likelihood x impact (1-5 each). Starting scores follow the rules:
a gap is likely (4), an open item possible (3), and impact follows the obligation's
severity. Change the scores, and record the treatment (mitigate, accept with a reason,
transfer, avoid), owner, due date and status. A 5x5 matrix shows where the open risks sit.

### Personal data discovery and inventory

Each engagement has a **Personal data** step. Upload a CSV or JSON export (up to 5 MB)
of customers, employees or patients, and a background scan reports which fields look
like personal data: Aadhaar (Verhoeff checksum), PAN, GSTIN (checksum), passport, voter
ID, driving licence, emails, Indian mobiles, UPI IDs, card numbers (Luhn), IP addresses,
plus fields known by their name (date of birth, health, biometric, salary, address).
Dates of birth and ages under 18 are flagged as children's data (Section 9).

A person confirms or rejects each finding (rejecting needs a reason). Confirmed fields
become records in the **Data inventory**, where you record the purpose, whose data it
is, the legal basis (consent or a legitimate use), retention, storage, recipients and
owner. Each record lists the obligations it touches, and the dashboard counts findings
waiting for review.

Privacy by design: the file is parsed in memory and never written to disk, at most 200
records are sampled per field, and only a masked *shape* of values (`Xxxxx Xxxxxx`,
`+99 99999 99999`) is stored. Nothing is sent to Claude. A finding means a field looks
like personal data; it is not a legal conclusion.

The built-in rules need nothing extra. For names and places inside free text, install
[Microsoft Presidio](https://github.com/microsoft/presidio) (MIT) and a spaCy model; the
scanner then uses both:

```
pip install -e ".[scanner]"
python -m spacy download en_core_web_sm    # or en_core_web_lg, more accurate
```

Synthetic sample files to try are linked on the Discovery tab
(`src/grc_agent/discovery/samples/`). Set `GRC_SCANNER=builtin` to skip Presidio.

### Privacy operations, compliance and risk

Each engagement groups its work in three rows under the workflow steps:

- **Privacy operations:** Personal data (discovery and inventory), **Consent** records,
  **Requests** from Data Principals (access, correction, erasure, grievance, nomination;
  response clock up to 90 days, Rule 14(3)), and **Breaches** (the Board's detailed report
  is due 72 hours after awareness, Rule 7(2)(b); each breach has a **Draft Board report**
  download), the **Erasure** log (Rule 8: the 48-hour notice before erasure is enforced)
  and breach **Drills** (time to a ready intimation and report, lessons).
- **Compliance:** the **Obligations** register (every duty in the DPDP Act and the Rules,
  with status, owner, re-check date, evidence, the penalty row at stake and a daily
  readiness trend; statuses are suggested from the intake and Controls until reviewed;
  `grc_agent/obligations.py`), **Accepted gaps** (reason, compensating measures, approver,
  review date), the **Readiness plan** (the client's obligations as steps in working
  order, from scoping through notice and consent, security and breaches, rights,
  retention, processors and transfers to delivery, each with one next action; progress is
  recalculated from live data on every visit, so it can't be ticked off by hand),
  **Controls** (the client's own status for every obligation; "not
  applicable" needs a reason and an admin, "implemented" needs evidence or a description),
  **Tasks**, the **Evidence** library, and **Policies** (version, approver, review date).
- **Risk:** the risk register, **Vendors & processors** (contract, data location, review),
  **DPIA**, **Systems** (each system holding personal data with Rule 6 safeguards), the
  data-flow map and connectors.

**Board inquiry pack:** one ZIP per client (Obligations page) with a cover note, the
obligations register, controls, every register, findings, the data inventory and map,
reviewed documents, the evidence files with SHA-256 hashes and the client's audit log.
Re-check dates that have come (obligations, controls, evidence) show on the Work queue.

Every register record has an owner, a due date, a status workflow that won't move on
without the facts it needs, a full history with comments, and evidence attachments.
"Create task" links on findings, risks, controls and inventory gaps turn a gap into
assigned work. The **Work queue** lists everything open across clients, most urgent
first; the **Audit log** shows every change; **Team & roles** sets each person's role.

### Organisations, teams and the owner's dashboard

GRC Flow is used two ways: a company runs its own DPDP compliance, and a consultancy or
MSSP (a partner) runs DPDP work for many companies. Each customer is an **organisation**,
a company or a partner, and sees only its own engagements (`web/access.py`).

- **Logging in** is two steps: *Log in as Company* or *Log in as Partner*, then the
  sign-in form. A partner account at the company door (or the other way round) is told
  which door to use and isn't signed in.
- **Team & roles** is each organisation's own page, run by its admins: **Admin**
  (everything, plus the team), **Manager** (does the work and signs off) and **Viewer**
  (reads, changes nothing). Invites go by email, or as a one-time link when email isn't
  set up. Nobody types a password for someone else.
- **People with access** on an engagement lets its lead bring in someone from outside
  the team, usually the client's own staff when a partner runs it, as a Manager or Viewer
  of that engagement only.
- **API keys and MCP** are for GRC Flow staff and a partner's admins and managers; the
  key reads only that partner's engagements.

The platform roles are hidden from customers: the **super admin** (the workspace owner,
the first account) and **GRC Flow admins** (staff, who see every engagement). The
super admin's dashboard at `/dashboard` is a separate page: nothing in the app links to
it, and anyone else gets "not found". There the owner creates organisations (company or
partner), makes one a **POC for any number of days** (1 to 365), extends it, makes it a
customer or ends it (its people are signed out until it's extended), invites people with
their team role, sets platform roles, and gives or removes access to engagements.

Older workspaces upgrade on start: each partner, client or trial account becomes its own
organisation (a trial's end date becomes the POC's) with that person as its admin.

The audit log is **tamper-evident**: each entry is sealed with a SHA-256 hash over its
own fields and the hash of the entry before it. **Check integrity** on the Audit log page
recomputes the whole chain and names the first entry that was changed, removed or
reordered outside the app; **Download CSV** exports the log with its hashes so an auditor
can recompute it independently (`db.entry_hash` shows the exact recipe). Entries written
before sealing existed are counted, not checked.

Evidence files (PDF, images, Word, Excel, CSV, text, JSON, up to 10 MB) are checked
against their extension, stored under a random name in `var/evidence/`, and only
downloadable by signed-in users. Back that folder up with the rest of `var/`.

**Evidence relevance check.** With an AI provider set up, the AI reads each file linked to
an obligation (text, PDF with a text layer, Word) and says whether it is about that
obligation: on topic, partly, not about it, or too little to judge, with a reason and what
it doesn't show. A file flagged as off-topic stops counting as evidence (a control can't
be marked implemented on it) until someone replaces it or clicks **Count it anyway**,
which is logged. The check runs after each upload and on demand; it judges only what a
document is about, never whether the control works. Images, spreadsheets and scanned
PDFs (no text layer) are labelled **Couldn't read the text** so a person checks them.

**Read text** on each evidence file shows exactly what GRC Flow read from it (text, PDF,
Word, CSV, JSON), as plain text, so a consultant can see what the AI check judged. Opening
it is recorded in the audit log.

**AI drafts, people decide.** Status and severity always come from the rules. When the AI
drafts a finding's wording, the finding is labelled **Human review required** until a
consultant reviews it, and the engagement can't be delivered until every AI-drafted
finding is reviewed. A draft marked **wrong** must be rewritten by a person (**Rewrite
this finding**), which also clears the draft pack so it's regenerated from the new
wording.

The demo notices (demo mode and demo tenant) appear on the GRC Analyst page only.

### GRC Analyst

The **GRC Analyst** page is an AI analyst that works from the workspace's live data. Pick
a client (or all clients) and ask: "What should I work on today?", "Summarise ENG-001 for
management", "What evidence should I request?", "Draft the audit report". It follows an
analyst's workflow (planning, fieldwork, evidence evaluation, risk assessment,
reporting), fetches the data before answering, and cites DPDP provisions from the
register and corpus. It also reads uploaded evidence (with the AI relevance check) and the
readiness plan. Its one action is **create_task**: when you ask it to, it adds tasks to a
client's Tasks register, labelled as drafted by the analyst and recorded under your name,
for a person to check. It never marks a finding, control or delivery, and it only sees
the clients the person asking can open. The **Analyst queue** lists the highest open risks across
clients, overdue first.

### MCP: use GRC Flow from Claude Desktop, Claude Code or Cursor

The app is also an **MCP server** at `/mcp`, so the AI app a consultant already uses can
read the workspace. Make a key on **API keys & MCP** (shown once; only its SHA-256 hash is
stored), then:

```bash
claude mcp add --transport http grc-flow https://app.grc-flow.com/mcp \
  --header "Authorization: Bearer YOUR_KEY"
```

For Claude Desktop and other apps, the page shows a config using `mcp-remote`. The app
gets the analyst's read-only tools (clients, findings, risks, data flows, evidence,
readiness plans, the obligations register and the Act's text), never `create_task`. A key
acts as its owner, works only in the `Authorization` header (never the browser cookie),
and stops working when revoked. Every call is recorded in the audit log as
`mcp_tool_called`.

### Demo tenant (sample data for demos)

Double-click **`start-demo.bat`** (or run `grc-web demo --open`) to open a separate demo
workspace on http://127.0.0.1:8001. Sign in as `demo` / `grc-demo-2026`.

- It lives in `./var-demo`, apart from your real workspace in `./var`, which it never touches.
  It refuses to seed into a folder that holds a real workspace.
- Six sample clients sit at every pipeline stage, from intake to delivered. They come with
  findings, documents, a risk register, data-flow maps, and AWS and GitHub evidence spread
  over the past month.
- It looks live. Every 40 seconds or so a colleague does something: re-syncs a connector,
  reviews a finding or picks up a risk. Notifications, pop-ups, the risk register and the
  data-flow map update while you present. "Run checks again" is simulated, and no real
  GitHub or AWS calls are made.
- `start-demo.bat --reset` starts from fresh sample data. `--live-seconds 0` turns the live
  activity off.
- Without any AI key, the analyst gives simulated answers in the demo. Pick a provider on the AI provider page to use a real model.
- **Share it on your office network:** double-click **`start-demo-lan.bat`** (or run
  `grc-web demo --lan --https`). It prints a link such as `https://192.168.1.20:8001` that
  anyone on the same Wi-Fi or LAN can open. No outside service is involved. If Windows
  Firewall asks, allow Python on **Private networks** only. The link only works inside
  your network and only while the window stays open. The certificate is self-signed, so
  each browser warns once: choose **Advanced > Proceed**. The connection is still
  encrypted, and the window prints the certificate's fingerprint so you can check it.

### Search and shortcuts

Press **Ctrl K** (or **/**) on any page to search pages and actions. Press **?** to see
all keyboard shortcuts, for example **g** then **d** for the dashboard and **n** for a
new engagement.

### Data manager

**Data manager** in the sidebar keeps an eye on what the workspace stores. It shows
the database size and health, the number of records and how many hold personal data,
and a catalogue of every table with its purpose and suggested retention. A watch
list flags tables missing from the catalogue, rows past their retention, wasted space
and when to back up. It only reads: back up by copying the data folder, or with your
PostgreSQL provider's backups.

See [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md) for how each request flows from click to feedback.

### Run it on your computer

You need Python 3.10 or newer ([python.org](https://www.python.org/downloads/); on
Windows, tick "Add python.exe to PATH"). Then, from the project folder:

| System | Command |
|---|---|
| macOS / Linux | `./start.sh` |
| Windows | double-click `start.bat`, or run `.\start.bat` in PowerShell (`start.bat` in Command Prompt) |

Something not starting on Windows? Run `check-setup.bat`. It lists the Pythons (and Node,
if installed) on the machine, shows which Python `start.bat` will use and whether `.venv`
still works, and changes nothing. If Python was upgraded or reinstalled, `start.bat`
rebuilds `.venv` on its own; your data in `.\var` is kept. The app needs only Python.
Node.js is optional and can be installed alongside it.

The first run sets up a `.venv`, installs the app, creates a `.env` from `.env.example`,
asks you to create your login, then opens http://127.0.0.1:8000 in your browser. Later
runs start in a few seconds. Press Ctrl+C to stop it. Add `--port 9000` to use another port.

To switch on the GRC Analyst and AI drafting, open **AI provider** in the sidebar
(`/settings/ai`). Pick a provider, paste its key and press **Test connection**.
Everything else in the app works without a key.

| Provider | Cost | Where to get a key |
|---|---|---|
| Anthropic Claude | Paid. Best answers | console.anthropic.com |
| Groq | Free tier, no card, very fast | console.groq.com/keys |
| Google Gemini | Free tier | aistudio.google.com/apikey |
| OpenRouter | Free `:free` models, 50 requests a day | openrouter.ai/keys |
| Cerebras, Mistral, NVIDIA NIM | Free tiers | their consoles |
| Ollama | Free, runs on your own computer | ollama.com |
| Any OpenAI-compatible server | Varies | LM Studio, vLLM, LiteLLM... |

Saving runs the test straight away. Providers retire free models every few months (Groq
shut down Llama 3.3 70B in August 2026). When that happens, the app reads the provider's
current model list, switches to a live model, remembers it, and says so. The Model box
suggests the provider's current models.

Saved keys are encrypted in the database and never shown again. You can also put a key in
`.env` instead (`GROQ_API_KEY=...`, `GEMINI_API_KEY=...`, `ANTHROPIC_API_KEY=...`). Then
the first key found is used, or the one `GRC_AI_PROVIDER` names. Free models are weaker
than Claude at legal reasoning, so review their drafted findings with extra care.

**No API key yet?** Add `GRC_AI_MODE=demo` to `.env` and restart. A built-in stand-in for
Claude then answers in the same format as the real API: the analyst calls the real tools,
and drafted findings go through the same citation checks. Its text is a placeholder, not AI.
A banner shows on every page, and an engagement with demo findings can't be delivered.
Remove the line once you have a key. Add more accounts with `.venv/bin/grc-web adduser NAME` (Windows PowerShell:
`.\.venv\Scripts\grc-web adduser NAME`), and change a password with `grc-web passwd NAME`.

Data (SQLite database and session secret) lives in `./var/`. It's git-ignored, so back up
that folder. To update, run `git pull` and then start the app again.

### HTTPS and security headers

On your own computer (`127.0.0.1`), plain HTTP is fine: the traffic never leaves the
machine. As soon as other people connect, use HTTPS so passwords and client data are
encrypted on the way. Three ways, from simplest to most robust:

1. **Built-in, self-signed:** `grc-web serve --lan --https`. The app makes a
   certificate in `var/tls/` (it covers `localhost`, the computer's name and its network
   address) and serves HTTPS itself. Browsers warn once because no public authority
   signed it.
2. **Your own certificate:** set `GRC_TLS_CERT` and `GRC_TLS_KEY` to the PEM files (from
   your company's certificate authority, or Let's Encrypt), then start the app as usual.
3. **Behind a reverse proxy** (Caddy, nginx, a cloud load balancer) that handles HTTPS: set
   `GRC_TRUSTED_PROXIES` to the proxy's address and `GRC_FORCE_HTTPS=1`. Plain-HTTP visits
   from outside your network are then redirected to HTTPS.

Whenever HTTPS is on, the login cookie is marked `Secure` (never sent over plain HTTP) and
browsers are told to keep using HTTPS (HSTS). Every page also sends a strict
Content-Security-Policy and clickjacking, sniffing and referrer protections. AI provider
keys are only ever sent over HTTPS, or over plain HTTP to a server on your own network
(such as Ollama).

### Deploy to your domain (for example app.grc-flow.com)

The app runs on its own subdomain, such as `https://app.grc-flow.com`, with a real
certificate (no browser warning), PostgreSQL for the data, and Caddy in front for HTTPS.
The same server can also run the website (`grc-flow.com`, from
[GRC-WEBSITE](https://github.com/Harshitahusts/GRC-WEBSITE)), linked to and from the app,
or leave the main domain free for a website hosted anywhere.

**Step-by-step for Oracle Cloud's free server in Mumbai:** [docs/DEPLOY_ORACLE.md](docs/DEPLOY_ORACLE.md).

On any Ubuntu server (Oracle, DigitalOcean, AWS...), the setup is one script once the
server exists and the subdomain's DNS A record points at it:

```bash
curl -fsSL https://raw.githubusercontent.com/Harshitahusts/GRC-Ai/main/deploy/setup-server.sh -o setup.sh
bash setup.sh                                   # installs Docker, opens ports, starts everything
cd ~/grc-flow && docker compose exec web grc-web adduser yourname   # your first login
```

It asks for the subdomain, the website's domain (optional) and a Groq key (optional),
generates the database password, and runs `compose.yaml` + `compose.postgres.yaml` +
`compose.caddy.yaml`, plus `compose.site.yaml` (the website) when a website domain
is given. Only Caddy is
reachable from the internet; the app and database have no public ports. Plain HTTP is
redirected to HTTPS, login cookies are `Secure`, and browsers are told to keep using HTTPS.
To update later, run `bash deploy/setup-server.sh` again in `~/grc-flow`: it keeps your
`.env` and data. Back up the Docker volumes `grc-data` and `grc-pg` regularly.

### Database: SQLite or PostgreSQL

By default everything (logins, client data, the audit log) lives in one SQLite file,
`var/grc.db`: nothing to install, one machine. For a shared server, use PostgreSQL:

1. Create an empty database (Docker, or a managed one such as RDS, Supabase or Neon).
2. Add it to `.env`:
   `GRC_DATABASE_URL=postgresql://grc:YOUR-PASSWORD@localhost:5432/grc`
3. Start the app as usual. `start.bat` / `start.sh` install PostgreSQL support; otherwise
   `pip install -e ".[postgres]"`. The tables are created on first start.
4. Already have data in SQLite? Copy it across once:
   `grc-web migrate-to-postgres` (the target must be empty; the SQLite file is kept as a
   backup). Ids are preserved, so every link between records survives.

The demo tenant uses its own schema (`grc_demo`) in the same database, so sample data
never mixes with real clients. The session secret, the encryption key for connector
secrets and uploaded evidence files stay in the data folder, so back that up as well as
the database. The Data manager page shows which database is in use (never its password).

With Docker: `docker compose -f compose.yaml -f compose.postgres.yaml up -d --build`
runs the app with a bundled PostgreSQL (set `POSTGRES_PASSWORD` in `.env` first).

To keep it running in the background and restart it after a reboot, use Docker:

```bash
docker compose up -d --build
docker compose exec web grc-web adduser harshit
```

### Corpus and Claude drafting

Put the DPDP Act and Rules PDFs from meity.gov.in in `corpus/`, list them in
`corpus/manifest.json`, then run `grc-corpus ingest` (steps in
[corpus/README.md](corpus/README.md)). After a restart, the app:

- checks every citation against the real Act and Rules instead of the sample index
- opens the text of any cited provision with one click, and searches provisions on the
  **Corpus** page
- offers **Run with Claude drafting** on the Findings tab. Rules still decide status and
  severity. Claude drafts each finding from the retrieved provisions and only the client
  answers tied to that obligation. A citation that doesn't resolve, or that points to a
  provision Claude wasn't shown, is flagged and blocks delivery. Needs `ANTHROPIC_API_KEY`
  in `.env`.

### Connectors

Each engagement has a **Connectors** tab for linking the client's systems. There are two
ways to connect each one:

**Quick: paste a read-only key** (the default; works straight away, no firm setup)

| Connector | What the client creates and you paste | What it checks |
|---|---|---|
| GitHub | A fine-grained personal access token with only *Metadata* and *Administration* read-only, plus the user or organisation name to check | Public repositories, default branch protection, secret scanning |
| AWS | An IAM user with only the AWS managed policy **SecurityAudit** (read-only) and an access key: the access key ID, the secret access key and the home region | **Where data is stored (India or not)**, S3 public access, root MFA, password policy, CloudTrail |

The keys are checked before they're saved, stored encrypted, and never shown again (only
the last four characters). Root account keys still work but are flagged with a warning.
The client removes access by deleting the token, or the access key or IAM user.

**Advanced: no keys shared** ("Or, without sharing keys" on each connector)

| Connector | How the client authorises |
|---|---|
| GitHub | Installs the firm's GitHub App, picks which repositories to share, approves read-only access on GitHub |
| AWS | Runs a CloudFormation template that creates a read-only role (AWS `SecurityAudit`) only the firm can use, then sends back the role ARN |

**More connectors** (paste read-only credentials the client creates; each one's page lists
the exact steps and permissions):

| Connector | Checks | Credentials |
|---|---|---|
| GitLab | Project visibility, default branch protection (gitlab.com or self-managed) | Access token with `read_api` |
| Bitbucket | Repository visibility, branch restrictions | Atlassian email + API token with read scopes |
| Google Cloud | Where Cloud Storage data is stored, public access prevention, uniform access | Service account key with Browser + Storage Object Viewer |
| Microsoft Azure | Where resources are located, public blob access, TLS on storage | App registration with the Reader role |
| Microsoft Entra ID | MFA coverage (admins without MFA fail), security defaults or Conditional Access, number of Global Administrators, guest accounts | App registration with Graph application permissions `User.Read.All`, `AuditLog.Read.All`, `Policy.Read.All`, `RoleManagement.Read.Directory` (the MFA report needs Entra ID P1/P2) |
| Slack, Teams, Google Chat | Post engagement updates (never client personal data) to a channel | Incoming webhook URL |

A check the credentials can't run (missing permission or licence) shows as "Couldn't check",
never as a pass. Identity, HR, ticketing, device and data-store systems are on the roadmap
and listed in one line on the Connectors page; until then, collect that evidence by hand.

One-time setup for the firm (advanced way only):

- **GitHub:** Connectors → GitHub → Connect → **Set up the GitHub App**. One click creates
  the app on GitHub; its private key is saved encrypted in `var/`. Back up `var/`. The app
  can also be supplied with `GRC_GITHUB_APP_ID`, `GRC_GITHUB_APP_SLUG` and
  `GRC_GITHUB_APP_PRIVATE_KEY` (the PEM, or a path to it).
- **AWS:** the firm's own AWS credentials must be on this computer (`aws configure`, the
  `AWS_*` variables, or a profile named in `GRC_AWS_PROFILE`). They need only
  `sts:AssumeRole`. Each engagement gets its own external ID, so a client's role works only
  for that engagement; access lasts 15 minutes per check run.

- Evidence is linked to provisions (Section 8(5) safeguards, Section 16(1) transfers) and
  shows under the matching findings.
- If AWS finds data outside India but the client answered "No" to using services outside
  India, the app flags the contradiction and blocks delivery until the answer is corrected.
- Clients remove access at any time: delete the token or access key, uninstall the GitHub
  App, or delete the CloudFormation stack.

### Data-flow map

Each agent-assisted engagement has a **Data flow** step: a live map of how the client's
personal data moves from people, through collection points and systems, to outside
companies, deletion and anywhere outside India.

- Built from the intake (data collected, tools and vendors, retention, children, foreign
  services), connector evidence (e.g. AWS regions outside India) and the findings. Each gap
  or open item is pinned to the step it affects, and flows are coloured by what they lead into.
- Animated: dots move along each flow. Pause it, or it stays still for people who prefer
  reduced motion.
- Live: the page re-checks every 10 seconds and redraws when the intake, the assessment or
  a connector check changes.
- Ask it questions: "What leaves India?", "Where does email go?", "Show issues", "Which
  vendors get data?". Matching steps are highlighted and the answer is written out.
- The **mitigation plan** lists every issue in the flow, most urgent first, with the fix.
  Download it as CSV.
- Add systems or vendors the intake missed. **Data flows** in the sidebar lists every
  client's map.

### Notifications

The bell in the sidebar tracks everything that happens: new engagements, intakes,
assessments (and unresolved citations), stale assessments, documents, delivery, connector
checks, failed logins and published content. Your own actions arrive already read, so the
count shows what others did and what the system found. New items pop up as they happen
(checked every 20 seconds). Filter by type, level or unread on the **Notifications** page.

The app binds to this machine only (127.0.0.1) by default. It uses the **sample** register
in `src/grc_agent/data/`. Point `GRC_REGISTER` and `GRC_CORPUS_INDEX` at the reviewed
register and the real corpus index before any client use.

## Using the agent from Python

```python
from grc_agent import Agent

agent = Agent()
print(agent.ask("Score a risk with likelihood 4 and impact 3").text)
print(agent.ask("Which DPDP obligation covers security safeguards?").text)  # same conversation
```

## Project layout

```
src/grc_agent/
  agent.py          agent loop: call Claude, run tools, repeat until done
  tools.py          tool definitions and handlers (add new tools here)
  prompts.py        system prompt
  config.py         settings from environment variables
  llm.py            free and other OpenAI-compatible AI providers
  citations.py      citation verifier: does a cited provision exist in the corpus?
  corpus/           Act and Rules ingestion (H2), search and lookup (H3), `grc-corpus`
  ai_assessment.py  Claude-drafted findings over the rule-based assessment (H6)
  web/              local web app (`grc-web`): FastAPI, templates, SQLite
  register.py       obligation register and intake questions
  assessment.py     rule-based gap assessment and readiness score
  documents.py      draft documents (gap report, RoPA, notice, playbook, DPA)
  risk.py           DPDP risk register (threats, likelihood x impact, treatments)
  plan.py           DPDP readiness plan: obligations as steps, next action for each
  evidence_check.py AI check that an evidence file is about its obligation
  web/mcp_views.py  API keys and the read-only MCP server (/mcp)
  discovery/        personal data scanner: India detectors, Presidio engine, file parsing
  web/registers.py  tasks, consent, requests, breaches, vendors, DPIAs, policies (one engine)
docs/RESEARCH.md    what we took from Probo, Openlane and CISO Assistant
tests/              unit tests (use a fake client; no API key needed)
```

## Configuration

Set in `.env` or the environment:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | API key (or use `ant auth login`) |
| `GRC_AGENT_MODEL` | provider default | Model name (`claude-opus-5`, `llama-3.3-70b-versatile`, ...) |
| `GRC_AGENT_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max` |
| `GRC_AGENT_MAX_TOKENS` | `16000` (4096 for others) | Max output tokens per response |
| `GRC_AGENT_MAX_TURNS` | `20` | Max model calls per question |
| `GRC_AI_MODE` | `api` | `demo` runs the offline stand-in for Claude, for testing without a key |
| `GRC_AI_PROVIDER` | first key found | `anthropic`, `groq`, `gemini`, `openrouter`, `cerebras`, `mistral`, `nvidia`, `ollama`, `openai`, `custom`. The AI provider page overrides it |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, ... | none | Keys for the free providers |
| `GRC_LLM_BASE_URL`, `GRC_LLM_API_KEY` | none | Address and key for `custom` (any OpenAI-compatible API) |
| `GRC_AWS_PROFILE` | none | AWS profile with the firm's credentials (for assuming clients' roles) |
| `GRC_PUBLIC_URL` | this server | Homepage shown on the GitHub App. An `https://` value also turns on Secure cookies |
| `GRC_TLS_CERT`, `GRC_TLS_KEY` | none | Certificate and key (PEM) to serve HTTPS directly |
| `GRC_FORCE_HTTPS` | off | `1` redirects plain-HTTP visits from outside your network to HTTPS |
| `GRC_TRUSTED_PROXIES` | `127.0.0.1` | Proxies whose `X-Forwarded-Proto` header is believed |
| `GRC_HTTPS` | off | `1` marks cookies Secure (set automatically by `--https`) |
| `GRC_SCANNER` | auto | `builtin` skips Presidio even when it is installed |
| `GRC_SCAN_MAX_MB` | `5` | Largest file the discovery scan accepts |
| `GRC_SCAN_SAMPLE_ROWS` | `200` | Records sampled per field |
| `GRC_EVIDENCE_MAX_MB` | `10` | Largest evidence file accepted |
| `GRC_DATABASE_URL` | none (SQLite) | `postgresql://…` to store everything in PostgreSQL |
| `GRC_DATABASE_SCHEMA` | `public` | PostgreSQL schema for this workspace |

The agent uses adaptive thinking, prompt caching, and server-side refusal fallbacks
(`fallbacks: "default"`).

## Adding a tool

1. Write a handler in `src/grc_agent/tools.py` that takes keyword arguments and returns a
   string or JSON-serializable value. Raise `ToolError` for errors the model should see.
2. Add a `Tool(...)` entry to `TOOLS` with a clear description and a JSON schema
   (`additionalProperties: false`, every property listed in `required`).
3. Add a test in `tests/test_tools.py`.

## Development

```bash
pytest          # run tests
ruff check .    # lint
ruff format .   # format
```

The obligations register is a sample. Check it against the gazetted Act and Rules before
relying on it with clients.
