"""Command-line entry point: `grc-agent "question"` or `grc-agent` for a chat REPL."""

from __future__ import annotations

import argparse
import sys

import anthropic

from grc_agent.agent import Agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grc-agent", description=__doc__)
    parser.add_argument("prompt", nargs="*", help="Ask a single question and exit.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show tool calls.")
    args = parser.parse_args(argv)

    agent = Agent()

    try:
        if args.prompt:
            _answer(agent, " ".join(args.prompt), args.verbose)
            return 0

        print("GRC agent. Type 'exit' to quit, 'reset' to start a new conversation.")
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if prompt in {"exit", "quit"}:
                return 0
            if prompt == "reset":
                agent.reset()
                continue
            if prompt:
                _answer(agent, prompt, args.verbose)
    except anthropic.AuthenticationError:
        print(
            "Authentication failed. Set ANTHROPIC_API_KEY or run `ant auth login`.", file=sys.stderr
        )
    except anthropic.RateLimitError:
        print("Rate limited by the API. Try again shortly.", file=sys.stderr)
    except anthropic.APIStatusError as exc:
        print(f"API error {exc.status_code}: {exc.message}", file=sys.stderr)
    except anthropic.APIConnectionError:
        print("Could not reach the Anthropic API. Check your network.", file=sys.stderr)
    return 1


def _answer(agent: Agent, prompt: str, verbose: bool) -> None:
    result = agent.ask(prompt)
    if verbose and result.tool_calls:
        print(f"[tools: {', '.join(result.tool_calls)}]")
    print(result.text)


if __name__ == "__main__":
    raise SystemExit(main())
