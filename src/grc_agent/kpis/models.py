"""Engagement records: the raw data every KPI is computed from.

One JSON file per engagement. See docs/kpis.md for the format and for the
definition of each field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MODES = {"agent", "manual"}
FINDING_STATUSES = {"gap", "compliant", "open_item", "not_applicable"}
VERDICTS = {"correct", "incomplete", "wrong"}
DOCUMENT_OUTCOMES = {"usable", "minor_edits", "material_edit", "full_rewrite"}


class EngagementFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Finding:
    id: str
    obligation_id: str
    status: str
    citations: tuple[str, ...]
    verdict: str | None = None
    hallucination: bool = False


@dataclass(frozen=True)
class Document:
    type: str
    outcome: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    @property
    def reviewed(self) -> bool:
        return bool(self.reviewed_by) and self.reviewed_at is not None and self.outcome is not None


@dataclass(frozen=True)
class Engagement:
    id: str
    client: str
    mode: str
    completed: bool
    consultant_hours: float | None = None
    register_size: int | None = None
    intake_completed_unaided: bool | None = None
    intake_submitted_at: datetime | None = None
    draft_pack_ready_at: datetime | None = None
    fell_back_to_manual: bool = False
    findings: tuple[Finding, ...] = ()
    documents: tuple[Document, ...] = ()

    @property
    def minutes_to_draft_pack(self) -> float | None:
        if self.intake_submitted_at is None or self.draft_pack_ready_at is None:
            return None
        return (self.draft_pack_ready_at - self.intake_submitted_at).total_seconds() / 60


def load_engagements(directory: str | Path) -> list[Engagement]:
    """Load every *.json engagement record in a directory, sorted by file name."""
    paths = sorted(Path(directory).glob("*.json"))
    engagements = [load_engagement(path) for path in paths]
    ids = [e.id for e in engagements]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise EngagementFormatError(f"Duplicate engagement ids: {', '.join(duplicates)}")
    return engagements


def load_engagement(path: str | Path) -> Engagement:
    path = Path(path)
    try:
        data = json.loads(path.read_text("utf-8"))
        return parse_engagement(data)
    except (json.JSONDecodeError, EngagementFormatError) as exc:
        raise EngagementFormatError(f"{path.name}: {exc}") from exc


def parse_engagement(data: dict[str, Any]) -> Engagement:
    r = _Reader(data, "engagement")
    engagement = Engagement(
        id=r.str("id"),
        client=r.str("client"),
        mode=r.choice("mode", MODES),
        completed=r.bool("completed"),
        consultant_hours=r.number("consultant_hours", optional=True),
        register_size=r.int("register_size", optional=True),
        intake_completed_unaided=r.bool("intake_completed_unaided", optional=True),
        intake_submitted_at=r.datetime("intake_submitted_at", optional=True),
        draft_pack_ready_at=r.datetime("draft_pack_ready_at", optional=True),
        fell_back_to_manual=r.bool("fell_back_to_manual", optional=True) or False,
        findings=tuple(_parse_finding(f, i) for i, f in enumerate(r.list("findings"))),
        documents=tuple(_parse_document(d, i) for i, d in enumerate(r.list("documents"))),
    )
    start, end = engagement.intake_submitted_at, engagement.draft_pack_ready_at
    if start and end:
        if (start.tzinfo is None) != (end.tzinfo is None):
            raise EngagementFormatError(
                "intake_submitted_at and draft_pack_ready_at must both include a timezone "
                "or both omit it"
            )
        if end < start:
            raise EngagementFormatError("draft_pack_ready_at is before intake_submitted_at")
    return engagement


def _parse_finding(data: Any, index: int) -> Finding:
    r = _Reader(data, f"findings[{index}]")
    citations = r.list("citations")
    if not all(isinstance(c, str) for c in citations):
        raise EngagementFormatError(f"findings[{index}].citations must be a list of strings")
    return Finding(
        id=r.str("id"),
        obligation_id=r.str("obligation_id"),
        status=r.choice("status", FINDING_STATUSES),
        citations=tuple(citations),
        verdict=r.choice("verdict", VERDICTS, optional=True),
        hallucination=r.bool("hallucination", optional=True) or False,
    )


def _parse_document(data: Any, index: int) -> Document:
    r = _Reader(data, f"documents[{index}]")
    return Document(
        type=r.str("type"),
        outcome=r.choice("outcome", DOCUMENT_OUTCOMES, optional=True),
        reviewed_by=r.str("reviewed_by", optional=True),
        reviewed_at=r.datetime("reviewed_at", optional=True),
    )


class _Reader:
    """Typed field access with error messages that name the offending field."""

    def __init__(self, data: Any, where: str) -> None:
        if not isinstance(data, dict):
            raise EngagementFormatError(f"{where} must be an object")
        self.data = data
        self.where = where

    def _get(self, key: str, optional: bool) -> Any:
        value = self.data.get(key)
        if value is None and not optional:
            raise EngagementFormatError(f"{self.where}.{key} is required")
        return value

    def _fail(self, key: str, expected: str) -> EngagementFormatError:
        return EngagementFormatError(f"{self.where}.{key} must be {expected}")

    def str(self, key: str, optional: bool = False) -> Any:
        value = self._get(key, optional)
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise self._fail(key, "a non-empty string")
        return value

    def bool(self, key: str, optional: bool = False) -> Any:
        value = self._get(key, optional)
        if value is not None and not isinstance(value, bool):
            raise self._fail(key, "true or false")
        return value

    def number(self, key: str, optional: bool = False) -> Any:
        value = self._get(key, optional)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        ):
            raise self._fail(key, "a non-negative number")
        return None if value is None else float(value)

    def int(self, key: str, optional: bool = False) -> Any:
        value = self._get(key, optional)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise self._fail(key, "a positive integer")
        return value

    def choice(self, key: str, options: set[str], optional: bool = False) -> Any:
        value = self._get(key, optional)
        if value is not None and value not in options:
            raise self._fail(key, "one of " + ", ".join(sorted(options)))
        return value

    def datetime(self, key: str, optional: bool = False) -> Any:
        value = self._get(key, optional)
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise self._fail(key, "an ISO 8601 date-time, e.g. 2026-10-05T14:30:00+05:30") from None

    def list(self, key: str) -> list[Any]:
        value = self.data.get(key, [])
        if not isinstance(value, list):
            raise self._fail(key, "a list")
        return value
