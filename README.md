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
