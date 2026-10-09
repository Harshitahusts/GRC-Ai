# GRC Flow (app)

DPDP Act compliance workspace for India: FastAPI + Jinja, SQLite or PostgreSQL. Live at
https://app.grc-flow.com; the website is the separate repo Harshitahusts/GRC-WEBSITE.

## How to work here

- Start every task with the `using-agent-skills` skill (`.claude/skills/`) and follow the
  skill it points to: spec before code for anything non-trivial, small verified slices,
  tests as proof, review before merge, `security-and-hardening` for auth, input or
  connector code. Shared checklists are in `.claude/references/`.
- The owner is not a developer: explain in plain words, step by step, and never ask them
  to paste keys or passwords into chat (keys go in the server's `.env` or the app's
  encrypted settings).
- Facts about the law must cite the DPDP Act 2023 or the final DPDP Rules 2025 (notified
  14 November 2025). Rule numbers from the January 2025 draft Rules differ; don't use them.

## Checks before a PR (CI runs the same, on Linux, Windows and PostgreSQL)

```bash
.venv/bin/ruff format --check src tests && .venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
```

Deploy: merging to main does not deploy unless the repo's deploy secrets are set; on the
server, `cd ~/grc-flow && bash deploy/setup-server.sh` updates everything.
