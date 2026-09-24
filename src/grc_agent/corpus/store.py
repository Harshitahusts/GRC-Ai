"""Load ingested chunks, look provisions up by citation, and search them (ticket H3).

Search is BM25 over each chunk's heading and text: no model download, same
results every time, and easy to inspect why a provision matched.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from grc_agent.corpus.ingest import Chunk
from grc_agent.kpis.citations import CorpusIndex, normalize_citation

_STOPWORDS = set(
    "a an and any are as at be by for from has have in is it its may of on or shall such that "
    "the this to under which with who whom whose will not no".split()
)


def _stem(word: str) -> str:
    for suffix in ("ies", "ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def tokenize(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS]


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


class Corpus:
    def __init__(self, chunks: list[Chunk], index: CorpusIndex, build_dir: Path | None = None):
        self.chunks = chunks
        self.index = index
        self.build_dir = build_dir
        self._by_key = {c.key: c for c in chunks}
        self._docs = [tokenize(f"{c.heading} {c.heading} {c.text}") for c in chunks]
        self._tf = [Counter(d) for d in self._docs]
        self._avg_len = sum(map(len, self._docs)) / max(len(self._docs), 1)
        df = Counter(term for doc in self._docs for term in set(doc))
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 5) -> list[Hit]:
        terms = tokenize(query)
        k1, b = 1.5, 0.75
        scored = []
        for chunk, tf, doc in zip(self.chunks, self._tf, self._docs, strict=True):
            score = 0.0
            for t in terms:
                if t in tf:
                    norm = tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(doc) / self._avg_len))
                    score += self._idf[t] * norm
            if score > 0:
                scored.append(Hit(chunk, score))
        scored.sort(key=lambda h: -h.score)
        return scored[:k]

    def provision(self, ref: str) -> list[Chunk]:
        """Chunks for a citation: the chunk, its sub-sections, or the chunk holding a clause."""
        key = normalize_citation(ref)
        if key is None:
            return []
        if key in self._by_key:
            return [self._by_key[key]]
        children = [c for c in self.chunks if c.key.startswith(key + "(")]
        if children:
            return children
        while key.endswith(")"):  # "section 8(6)(a)" lives in "section 8(6)"
            key = key[: key.rindex("(")]
            if key in self._by_key:
                return [self._by_key[key]]
        return []


def default_build_dir() -> Path:
    return Path(os.getenv("GRC_CORPUS_DIR", "corpus")) / "build"


def load_corpus(build_dir: str | Path | None = None) -> Corpus | None:
    """Load an ingested corpus, or None if it hasn't been built yet."""
    build = Path(build_dir) if build_dir else default_build_dir()
    chunks_path = build / "chunks.jsonl"
    if not chunks_path.exists():
        return None
    chunks = [
        Chunk(**json.loads(line)) for line in chunks_path.read_text("utf-8").splitlines() if line
    ]
    return Corpus(chunks, CorpusIndex.from_file(build / "corpus_index.txt"), build)


def evaluate(corpus: Corpus, questions: list[dict], k: int = 3) -> dict:
    """H3 check: does the expected provision come back in the top k?

    A hit is a retrieved chunk that is the expected provision or sits inside it
    (for example "Section 8(6)" when "Section 8" is expected).
    """
    rows = []
    for q in questions:
        expected = [normalize_citation(e) for e in q["expected"]]
        got = [h.chunk.ref for h in corpus.search(q["question"], k)]
        hit = any(
            e and (g_key == e or g_key.startswith(e + "("))
            for g in got
            for g_key in [normalize_citation(g) or ""]
            for e in expected
        )
        rows.append({"question": q["question"], "expected": q["expected"], "got": got, "hit": hit})
    hits = sum(r["hit"] for r in rows)
    return {"hits": hits, "total": len(rows), "k": k, "rows": rows}
