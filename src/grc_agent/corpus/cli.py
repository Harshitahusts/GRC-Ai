"""`grc-corpus`: build and query the DPDPA corpus.

grc-corpus ingest                 parse corpus/manifest.json sources into corpus/build/
grc-corpus search "breach"        top provisions for a question
grc-corpus show "Section 8(6)"    print a provision's text
grc-corpus eval questions.json    retrieval check: expected provision in the top 3?
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from grc_agent.corpus.ingest import ingest
from grc_agent.corpus.store import evaluate, load_corpus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grc-corpus", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--corpus-dir",
        default=os.getenv("GRC_CORPUS_DIR", "corpus"),
        help="Folder with manifest.json and the source files (default: ./corpus).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="Parse the sources and write corpus/build/.")
    search = sub.add_parser("search", help="Search provisions.")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5)
    show = sub.add_parser("show", help="Print a provision.")
    show.add_argument("ref")
    ev = sub.add_parser("eval", help="Check retrieval against known answers.")
    ev.add_argument(
        "questions", help='JSON list of {"question": ..., "expected": ["Section 5(1)"]}'
    )
    ev.add_argument("-k", type=int, default=3)
    args = parser.parse_args(argv)
    corpus_dir = Path(args.corpus_dir)

    if args.command == "ingest":
        try:
            report = ingest(corpus_dir)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for s in report.sources:
            span = f"{s.first} .. {s.last}"
            print(f"{s.file} ({s.kind}): {s.units} numbered units, {s.chunks} chunks, {span}")
            if s.missing:
                print(f"  MISSING: {', '.join(s.missing)}")
            for w in s.warnings:
                print(f"  WARNING: {w}")
        print(f"Total: {report.chunks} chunks, {report.index_entries} citable references.")
        if report.unresolved_chunks:
            print(f"  UNRESOLVED chunk references: {', '.join(report.unresolved_chunks)}")
        print(
            f"Written to {corpus_dir / 'build'}. "
            + ("OK" if report.ok else "CHECK THE ISSUES ABOVE")
        )
        return 0 if report.ok else 1

    corpus = load_corpus(corpus_dir / "build")
    if corpus is None:
        print(
            f"error: no corpus built yet. Run: grc-corpus --corpus-dir {corpus_dir} ingest",
            file=sys.stderr,
        )
        return 2

    if args.command == "search":
        for hit in corpus.search(args.query, args.k):
            c = hit.chunk
            print(f"{hit.score:6.2f}  {c.ref}  {c.heading}\n        {c.text[:160]}")
        return 0
    if args.command == "show":
        chunks = corpus.provision(args.ref)
        if not chunks:
            print(f"{args.ref}: not in the corpus", file=sys.stderr)
            return 1
        for c in chunks:
            print(f"{c.ref} ({c.heading}) [{c.source}, page {c.page}]\n{c.text}\n")
        return 0

    result = evaluate(corpus, json.loads(Path(args.questions).read_text("utf-8")), args.k)
    for row in result["rows"]:
        mark = "HIT " if row["hit"] else "MISS"
        expected, got = ", ".join(row["expected"]), ", ".join(row["got"])
        print(f"{mark} {row['question']}\n     expected {expected}; got {got}")
    score = f"{result['hits']}/{result['total']}"
    print(f"\n{score} with the expected provision in the top {result['k']}.")
    target = 0.9 * result["total"]
    print(
        "Meets the H3 bar (9 of 10)." if result["hits"] >= target else "Below the H3 bar (9 of 10)."
    )
    return 0 if result["hits"] >= target else 1


if __name__ == "__main__":
    raise SystemExit(main())
