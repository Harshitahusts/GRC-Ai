"""`grc-kpis ENGAGEMENTS_DIR --corpus-index FILE`: print the KPI scorecard."""

from __future__ import annotations

import argparse
import json
import sys

from grc_agent.kpis.citations import CorpusIndex
from grc_agent.kpis.models import EngagementFormatError, load_engagements
from grc_agent.kpis.scorecard import build_scorecard, render_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grc-kpis", description=__doc__)
    parser.add_argument("engagements", help="Directory of engagement *.json records.")
    parser.add_argument(
        "--corpus-index",
        required=True,
        help="File listing every provision in the corpus, one citation per line.",
    )
    parser.add_argument("--json", action="store_true", help="Print the scorecard as JSON.")
    args = parser.parse_args(argv)

    try:
        index = CorpusIndex.from_file(args.corpus_index)
        engagements = load_engagements(args.engagements)
    except (OSError, ValueError, EngagementFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    card = build_scorecard(engagements, index)
    print(json.dumps(card.to_dict(), indent=2) if args.json else render_text(card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
