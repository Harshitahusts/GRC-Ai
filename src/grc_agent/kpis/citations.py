"""Deterministic citation verifier: does a cited provision exist in the corpus?

This is a string match against an index of provisions, never a model call.
Citations are normalized first so formatting differences ("Sec. 5 (1)" vs
"Section 5(1)") don't count as failures, but anything that can't be parsed
or isn't in the index is unresolved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_KINDS = {
    "section": "section",
    "sec": "section",
    "s": "section",
    "rule": "rule",
    "r": "rule",
    "schedule": "schedule",
}

_CITATION = re.compile(
    r"""^\s*
    (?P<kind>section|sec|s|rule|r|schedule)\.?\s*
    (?P<number>\d+[a-z]?)?\s*
    (?P<subs>(?:\(\s*[0-9a-z]+\s*\)\s*)*)
    $""",
    re.IGNORECASE | re.VERBOSE,
)


def normalize_citation(ref: str) -> str | None:
    """Return the canonical form of a citation, or None if it can't be parsed.

    Canonical form is lowercase with no spaces inside brackets, for example
    "section 5(1)(a)", "rule 3(b)", "schedule".
    """
    match = _CITATION.match(ref)
    if match is None:
        return None
    kind = _KINDS[match["kind"].lower()]
    number = (match["number"] or "").lower()
    subs = re.findall(r"\(\s*([0-9a-z]+)\s*\)", match["subs"].lower())
    if not number and (kind != "schedule" or subs):
        return None
    head = f"{kind} {number}" if number else kind
    return head + "".join(f"({s})" for s in subs)


def _with_parents(canonical: str) -> list[str]:
    """ "section 5(1)(a)" -> ["section 5(1)(a)", "section 5(1)", "section 5"]."""
    out = [canonical]
    while canonical.endswith(")"):
        canonical = canonical[: canonical.rindex("(")]
        out.append(canonical)
    return out


@dataclass(frozen=True)
class CitationCheck:
    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return bool(self.resolved) and not self.unresolved


class CorpusIndex:
    """The set of provisions that exist in the ingested corpus.

    Adding "Section 5(1)(a)" also adds its parents "Section 5(1)" and
    "Section 5", since a parent citation is valid wherever a child exists.
    A child is never inferred from a parent.
    """

    def __init__(self, refs: Iterable[str]) -> None:
        self._refs: set[str] = set()
        for ref in refs:
            canonical = normalize_citation(ref)
            if canonical is None:
                raise ValueError(f"Corpus index entry is not a valid citation: {ref!r}")
            self._refs.update(_with_parents(canonical))

    @classmethod
    def from_file(cls, path: str | Path) -> CorpusIndex:
        """One citation per line; blank lines and lines starting with # are ignored."""
        lines = Path(path).read_text("utf-8").splitlines()
        return cls(line.strip() for line in lines if line.strip() and not line.startswith("#"))

    def __len__(self) -> int:
        return len(self._refs)

    def resolves(self, ref: str) -> bool:
        canonical = normalize_citation(ref)
        return canonical is not None and canonical in self._refs

    def check(self, refs: Iterable[str]) -> CitationCheck:
        resolved, unresolved = [], []
        for ref in refs:
            (resolved if self.resolves(ref) else unresolved).append(ref)
        return CitationCheck(tuple(resolved), tuple(unresolved))
