"""The DPDPA corpus: ingestion (H2), retrieval (H3) and provision lookup.

Sources (the Act and the Rules as published by MeitY) are listed in
corpus/manifest.json. Ingestion splits them into chunks that each carry a
citation such as "Section 8(6)", and writes the corpus index the citation
verifier checks against.
"""

from grc_agent.corpus.ingest import Chunk, IngestReport, ingest
from grc_agent.corpus.store import Corpus, load_corpus

__all__ = ["Chunk", "Corpus", "IngestReport", "ingest", "load_corpus"]
