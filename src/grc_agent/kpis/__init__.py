"""KPI tracking for the DPDPA agent pilot (build plan, Step 7).

Engagement records go in, a scorecard comes out: the North Star count
(Verified Engagements Delivered) plus each pilot KPI against its threshold.
Definitions of every term live in docs/kpis.md.
"""

from grc_agent.kpis.citations import CorpusIndex, normalize_citation
from grc_agent.kpis.models import Document, Engagement, Finding, load_engagements
from grc_agent.kpis.scorecard import Scorecard, build_scorecard

__all__ = [
    "CorpusIndex",
    "Document",
    "Engagement",
    "Finding",
    "Scorecard",
    "build_scorecard",
    "load_engagements",
    "normalize_citation",
]
