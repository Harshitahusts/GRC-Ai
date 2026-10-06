"""Built-in detectors for personal data common in Indian datasets.

Each detector matches a single value. Where an identifier has a checksum (Aadhaar's
Verhoeff digit, GSTIN's mod-36 digit, card numbers' Luhn digit) the checksum must pass,
so random numbers are not reported as identifiers. A match is a *potential* finding,
never proof of identity.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------- checksums

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_ok(value: str) -> bool:
    total = 0
    for i, ch in enumerate(value[:14]):
        product = _GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _GST_CHARS[(36 - total % 36) % 36] == value[14]


# ---------------------------------------------------------------- detectors


@dataclass(frozen=True)
class Detector:
    entity: str
    pattern: re.Pattern
    score: float
    check: Callable[[str], bool] | None = None
    # Whole value must match (identifiers) or it may appear inside text (emails, phones).
    anywhere: bool = False


_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"


_HEALTH_WORDS = re.compile(
    r"\b(diagnos(?:ed|is)|dyslexi[ac]|adhd|autis(?:m|tic)|asthma|diabet(?:es|ic)|epilep(?:sy|tic)"
    r"|allerg(?:y|ic)|disabilit(?:y|ies)|disabled|pregnan(?:t|cy)|hiv|cancer|chemotherapy"
    r"|depression|anxiety disorder|mental health|medication|prescri(?:bed|ption)|therapy"
    r"|blood group|surgery|hospitali[sz]ed)\b",
    re.IGNORECASE,
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


DETECTORS: tuple[Detector, ...] = (
    Detector(
        "EMAIL_ADDRESS",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
        0.95,
        anywhere=True,
    ),
    Detector(
        "IN_AADHAAR",
        re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"),
        0.9,
        check=lambda m: verhoeff_ok(_digits(m)),
        anywhere=True,
    ),
    Detector(
        "PHONE_NUMBER",
        re.compile(r"(?<![\d+])(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)"),
        0.8,
        anywhere=True,
    ),
    Detector("IN_PAN", re.compile(r"[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z]"), 0.9),
    Detector(
        "IN_GSTIN",
        re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]"),
        0.95,
        check=gstin_ok,
    ),
    Detector("IN_VOTER", re.compile(r"[A-Z]{3}\d{7}"), 0.6),
    Detector("IN_PASSPORT", re.compile(r"[A-PR-WY][1-9]\d ?\d{4}[1-9]"), 0.6),
    Detector(
        "IN_DRIVING_LICENCE",
        re.compile(r"[A-Z]{2}[ -]?\d{2}[ -]?(?:19|20)\d{2}\d{7}"),
        0.7,
    ),
    Detector(
        "CREDIT_CARD",
        re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
        0.85,
        check=lambda m: luhn_ok(_digits(m)) and 13 <= len(_digits(m)) <= 19,
        anywhere=True,
    ),
    Detector("UPI_ID", re.compile(r"[A-Za-z0-9._-]{2,64}@[A-Za-z]{2,32}"), 0.8),
    Detector("IP_ADDRESS", re.compile(rf"(?:{_OCTET}\.){{3}}{_OCTET}"), 0.7),
    # Health details typed into free text (notes, comments). Only clear medical words, so
    # ordinary notes don't trip it; a person confirms every finding anyway.
    Detector("HEALTH", _HEALTH_WORDS, 0.6, anywhere=True),
)


def detect(value: str) -> dict[str, float]:
    """Entity -> score for everything the built-in detectors find in one value."""
    text = value.strip()
    found: dict[str, float] = {}
    if not text:
        return found
    for d in DETECTORS:
        if d.anywhere:
            for m in d.pattern.finditer(text):
                if d.check is None or d.check(m.group(0)):
                    found[d.entity] = max(found.get(d.entity, 0), d.score)
                    break
        elif d.pattern.fullmatch(text) and (d.check is None or d.check(text)):
            found[d.entity] = max(found.get(d.entity, 0), d.score)
    # An Aadhaar number also looks like a card number or phone digits; keep the checksummed one.
    if "IN_AADHAAR" in found:
        found.pop("CREDIT_CARD", None)
        found.pop("PHONE_NUMBER", None)
    return found
