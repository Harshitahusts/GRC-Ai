# GRC-Ai

A starter AI agent for **governance, risk, and compliance (GRC)** work, built on the
[Claude API](https://docs.claude.com) with the official `anthropic` Python SDK.

The agent chats with you, and when it needs facts it calls local tools: it searches a
control catalog (mapped to ISO/IEC 27001, SOC 2, and NIST CSF) and scores risks on a
5x5 matrix.

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

A local web app for running DPDPA engagements end to end. You log in and work from a
dashboard. Each engagement follows these steps:

1. **Intake:** business-language questions, with follow-ups that appear only when relevant.
2. **Findings:** a rule-based assessment gives one finding per obligation, and each
   finding's citation is checked against the corpus. The findings page is also where the
   consultant scores accuracy.
3. **Documents:** a gap report, RoPA, privacy notice, breach playbook and a DPA marked
   draft-for-lawyer. A person has to review each one before it can be downloaded as .docx.
4. **Delivery:** blocked until every check passes (the plan's "hard stop"), with no override.

The dashboard shows the North Star, the pilot KPIs and recent activity. There's also a
Claude assistant page.

### Run it on your computer

You need Python 3.10 or newer ([python.org](https://www.python.org/downloads/); on
Windows, tick "Add python.exe to PATH"). Then, from the project folder:

| System | Command |
|---|---|
| macOS / Linux | `./start.sh` |
| Windows | double-click `start.bat`, or run `.\start.bat` in PowerShell (`start.bat` in Command Prompt) |

The first run sets up a `.venv`, installs the app, creates a `.env` from `.env.example`,
asks you to create your login, then opens http://127.0.0.1:8000 in your browser. Later
runs start in a few seconds. Press Ctrl+C to stop it. Add `--port 9000` to use another port.

Put your `ANTHROPIC_API_KEY` in `.env` to enable the assistant page and Claude drafting.
Everything else works without it.

**No API key yet?** Add `GRC_AI_MODE=demo` to `.env` and restart. A built-in stand-in for
Claude then answers in the same format as the real API: the assistant calls the real tools,
and drafted findings go through the same citation checks. Its text is a placeholder, not AI.
A banner shows on every page, and an engagement with demo findings can't be delivered.
Remove the line once you have a key. Add more accounts with `.venv/bin/grc-web adduser NAME` (Windows PowerShell:
`.\.venv\Scripts\grc-web adduser NAME`), and change a password with `grc-web passwd NAME`.

Data (SQLite database and session secret) lives in `./var/`. It's git-ignored, so back up
that folder. To update, run `git pull` and then start the app again.

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

### Docs and blog

**Docs & blog** in the app is where you write and publish DPDPA guides and SEO articles in
Markdown. The editor shows a live SEO checklist: title and description length, focus
keyword placement, word count, subheadings, internal links, and links to unpublished pages.

- It ships with 13 DPDPA docs and 2 blog posts, imported once as **drafts**. They were
  written from general knowledge of the Act and Rules, so check every point and section
  reference against the gazetted text before publishing.
- Publishing needs a named reviewer.
- Published items appear on public pages that need no login: `/docs`, `/blog`, and
  `/sitemap.xml` and `/robots.txt` for search engines. Every page has its own title,
  description, canonical link, Open Graph tags and article structured data.
- Search engines can only find these pages once the site is hosted at a public address.
  Set `GRC_PUBLIC_URL` (for example `https://www.example.in`) so canonical links and the
  sitemap use it, and `GRC_SITE_NAME` for the site name.

The app binds to this machine only (127.0.0.1) by default. It uses the **sample** register
in `src/grc_agent/data/`. Point `GRC_REGISTER` and `GRC_CORPUS_INDEX` at the reviewed
register and the real corpus index before any client use.

## Usage

```bash
grc-agent "Which controls cover MFA, and how do they map to SOC 2?"
grc-agent -v                     # interactive chat; -v shows tool calls
```

From Python:

```python
from grc_agent import Agent

agent = Agent()
print(agent.ask("Score a risk with likelihood 4 and impact 3").text)
print(agent.ask("What controls would reduce it?").text)  # same conversation
```

## Pilot KPIs

`grc-kpis` scores the DPDPA pilot against the build plan's KPIs: the North Star
(**Verified Engagements Delivered**) and eight pilot thresholds, including zero
fabricated citations. For each engagement that doesn't count yet, it lists what's blocking it.

```bash
grc-kpis examples/kpis/engagements --corpus-index examples/kpis/corpus_index.sample.txt
```

See [docs/kpis.md](docs/kpis.md) for each metric, the draft term dictionary, and the
engagement record format.

## Project layout

```
src/grc_agent/
  agent.py          agent loop: call Claude, run tools, repeat until done
  tools.py          tool definitions and handlers (add new tools here)
  prompts.py        system prompt
  config.py         settings from environment variables
  cli.py            `grc-agent` command
  kpis/             citation verifier, engagement records, KPI scorecard, `grc-kpis`
  corpus/           Act and Rules ingestion (H2), search and lookup (H3), `grc-corpus`
  ai_assessment.py  Claude-drafted findings over the rule-based assessment (H6)
  web/              local web app (`grc-web`): FastAPI, templates, SQLite
  register.py       obligation register and intake questions
  assessment.py     rule-based gap assessment and readiness score
  documents.py      draft documents (gap report, RoPA, notice, playbook, DPA)
  data/controls.json  sample control catalog
tests/              unit tests (use a fake client; no API key needed)
docs/kpis.md        KPI definitions and dictionary
examples/kpis/      fictional engagement records and a sample corpus index
```

## Configuration

Set in `.env` or the environment:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | API key (or use `ant auth login`) |
| `GRC_AGENT_MODEL` | `claude-opus-5` | Claude model |
| `GRC_AGENT_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max` |
| `GRC_AGENT_MAX_TOKENS` | `16000` | Max output tokens per response |
| `GRC_AGENT_MAX_TURNS` | `20` | Max model calls per question |
| `GRC_AI_MODE` | `api` | `demo` runs the offline stand-in for Claude, for testing without a key |

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

The control catalog is sample data with our own short summaries and approximate
framework mappings. Check the official framework documents before relying on it.
