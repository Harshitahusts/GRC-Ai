"""Scanner engines: Microsoft Presidio when it is installed, built-in rules otherwise.

Presidio (MIT licence) adds named-entity recognition, so it can spot names and places
inside free text. The built-in rules cover Indian identifiers with checksums and run
everywhere with no extra download. Presidio runs *alongside* the built-in rules, never
instead of them.

Install Presidio with:  pip install -e ".[scanner]"
then a spaCy model:     python -m spacy download en_core_web_sm   (or en_core_web_lg)

Set GRC_SCANNER=builtin to force the built-in rules even when Presidio is installed.
All processing is local. No value is sent to any external service.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

from grc_agent.discovery import detectors

# Presidio entities worth reporting for Indian datasets, mapped to our kinds.
PRESIDIO_MAP = {
    "PERSON": "PERSON",
    "EMAIL_ADDRESS": "EMAIL_ADDRESS",
    "PHONE_NUMBER": "PHONE_NUMBER",
    "LOCATION": "ADDRESS",
    "CREDIT_CARD": "CREDIT_CARD",
    "IP_ADDRESS": "IP_ADDRESS",
    "NRP": "NRP",
    "IN_AADHAAR": "IN_AADHAAR",
    "IN_PAN": "IN_PAN",
    "IN_PASSPORT": "IN_PASSPORT",
    "IN_VOTER": "IN_VOTER",
    "IN_GSTIN": "IN_GSTIN",
    "IN_VEHICLE_REGISTRATION": "IN_VEHICLE_REGISTRATION",
}
# Presidio's phone and location guesses are noisy on short values; require more certainty.
MIN_SCORE = {"PHONE_NUMBER": 0.6, "ADDRESS": 0.6, "NRP": 0.7}
SPACY_MODELS = ("en_core_web_lg", "en_core_web_md", "en_core_web_sm")


@dataclass
class Engine:
    name: str
    description: str
    _analyzer: object | None = None

    def detect(self, value: str) -> dict[str, float]:
        found = detectors.detect(value)
        if self._analyzer is None or not any(c.isalpha() for c in value):
            return found
        results = self._analyzer.analyze(  # type: ignore[attr-defined]
            text=value[:2000], language="en", entities=list(PRESIDIO_MAP), score_threshold=0.4
        )
        for r in results:
            kind = PRESIDIO_MAP[r.entity_type]
            if r.score >= MIN_SCORE.get(kind, 0.4):
                found[kind] = max(found.get(kind, 0.0), round(float(r.score), 2))
        return found


_lock = threading.Lock()
_cached: Engine | None = None

BUILTIN = Engine(
    "builtin",
    "Built-in rules for Indian identifiers, emails and phone numbers. Install Presidio to "
    "also find names and addresses in free text.",
)


def _presidio() -> Engine | None:
    try:
        import spacy.util
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_analyzer.predefined_recognizers import (
            InAadhaarRecognizer,
            InGstinRecognizer,
            InPanRecognizer,
            InPassportRecognizer,
            InVehicleRegistrationRecognizer,
            InVoterRecognizer,
        )
    except ImportError:
        return None
    model = next((m for m in SPACY_MODELS if spacy.util.is_package(m)), None)
    if model is None:
        return None
    # tldextract (used by the URL recognizer) tries to download a suffix list and falls
    # back to its bundled copy when offline; keep that from filling the log.
    logging.getLogger("tldextract").setLevel(logging.CRITICAL)
    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)
    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": model}],
        }
    ).create_engine()
    registry = RecognizerRegistry(supported_languages=["en"])
    registry.load_predefined_recognizers(languages=["en"], nlp_engine=nlp)
    for rec in (
        InAadhaarRecognizer,
        InGstinRecognizer,
        InPanRecognizer,
        InPassportRecognizer,
        InVehicleRegistrationRecognizer,
        InVoterRecognizer,
    ):
        registry.add_recognizer(rec())
    analyzer = AnalyzerEngine(registry=registry, nlp_engine=nlp, supported_languages=["en"])
    return Engine(
        "presidio",
        f"Microsoft Presidio ({model}) with India recognizers, plus the built-in rules.",
        analyzer,
    )


def get_engine() -> Engine:
    """The best engine available, built once per process."""
    global _cached
    if os.getenv("GRC_SCANNER", "").lower() == "builtin":
        return BUILTIN
    with _lock:
        if _cached is None:
            try:
                _cached = _presidio() or BUILTIN
            except Exception:  # a broken Presidio install must not break scanning
                logging.getLogger(__name__).exception("Presidio failed to load")
                _cached = BUILTIN
        return _cached


def presidio_installed() -> bool:
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        return False
    return True
