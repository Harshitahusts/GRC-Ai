"""Read an uploaded CSV or JSON file and find the fields that hold personal data.

Only a sample of rows is examined (GRC_SCAN_SAMPLE_ROWS, default 200). The raw file is
read in memory and never written to disk; what is kept is per-field metadata: which kinds
of personal data were found, in what share of the sampled values, and the *shape* of
a couple of values (e.g. "Xxxxx Xxxxxx", "+99 99999 99999") so a reviewer can see what
was matched without the value itself.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from grc_agent.discovery.catalog import KINDS, RISK_ORDER, hint_for
from grc_agent.discovery.engine import Engine

MAX_BYTES = int(float(os.getenv("GRC_SCAN_MAX_MB", "5")) * 1024 * 1024)
SAMPLE_ROWS = int(os.getenv("GRC_SCAN_SAMPLE_ROWS", "200"))
# Share of non-empty sampled values that must match before a field is reported on
# values alone (a matching column name lowers the bar).
VALUE_THRESHOLD = 0.3


class ScanInputError(ValueError):
    """The file can't be scanned; the message is safe to show the user."""


@dataclass
class Table:
    kind: str  # csv | json
    columns: list[str]
    rows: int
    samples: dict[str, list[str]]  # column -> sampled non-empty values


@dataclass
class ColumnResult:
    column: str
    entities: dict[str, float]  # kind -> share of sampled values matched (0..1)
    primary: str
    category: str
    risk: str
    confidence: str  # high | medium | low
    match_ratio: float
    sampled: int
    shapes: list[str] = field(default_factory=list)
    minors: int = 0  # values showing a person under 18
    name_hint: str | None = None

    @property
    def label(self) -> str:
        return KINDS[self.primary].label


# ---------------------------------------------------------------- parsing


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ScanInputError("The file isn't readable text. Save it as UTF-8 CSV or JSON.")


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(_cell(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v)
    return str(v)


def _flatten(obj: dict, prefix: str = "", depth: int = 0) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 2:
            out.update(_flatten(v, f"{key}.", depth + 1))
        else:
            out[key] = _cell(v)
    return out


def _records_from_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # JSON Lines: one object per line.
        try:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as e:
            raise ScanInputError(f"The JSON isn't valid (line {e.lineno}).") from None
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) != 1:
            raise ScanInputError(
                "Upload a JSON array of records, or an object holding one array of records."
            )
        data = lists[0]
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ScanInputError("The JSON must be a list of objects (one object per record).")
    return data


def _sample_indices(total: int, n: int) -> set[int]:
    if total <= n:
        return set(range(total))
    step = total / n
    return {int(i * step) for i in range(n)}


def read_table(filename: str, data: bytes) -> Table:
    """Parse an upload into sampled columns. Raises ScanInputError with a friendly message."""
    if not data:
        raise ScanInputError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ScanInputError(
            f"The file is over {MAX_BYTES // (1024 * 1024)} MB. Upload a sample of the records."
        )
    ext = os.path.splitext(filename.lower())[1]
    if ext not in (".csv", ".json", ".jsonl"):
        raise ScanInputError("Upload a .csv or .json file.")
    text = _decode(data)
    if ext == ".csv":
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        try:
            header = next(reader)
        except StopIteration:
            raise ScanInputError("The file is empty.") from None
        columns = [h.strip() or f"column_{i + 1}" for i, h in enumerate(header)]
        if len(columns) < 1 or not any(columns):
            raise ScanInputError("The CSV needs a header row naming each column.")
        rows = list(reader)
        records = [
            {c: (row[i] if i < len(row) else "") for i, c in enumerate(columns)}
            for row in rows
            if any(cell.strip() for cell in row)
        ]
        kind = "csv"
    else:
        records = [_flatten(r) for r in _records_from_json(text)]
        columns = list(dict.fromkeys(k for r in records for k in r))
        kind = "json"
    if not records:
        raise ScanInputError("The file has a header but no records.")
    if len(columns) > 500:
        raise ScanInputError("The file has more than 500 columns. Upload fewer fields.")
    keep = _sample_indices(len(records), SAMPLE_ROWS)
    samples: dict[str, list[str]] = {c: [] for c in columns}
    for i, r in enumerate(records):
        if i in keep:
            for c in columns:
                v = str(r.get(c, "")).strip()
                if v:
                    samples[c].append(v)
    return Table(kind=kind, columns=columns, rows=len(records), samples=samples)


# ---------------------------------------------------------------- analysis


def shape(value: str) -> str:
    """A value's shape with every letter and digit hidden: 'Priya S.' -> 'Xxxxx X.'."""
    out = re.sub(r"[A-Z]", "X", value[:40])
    out = re.sub(r"[a-z]", "x", out)
    out = re.sub(r"\d", "9", out)
    out = re.sub(r"[^\x00-\x7f]", "*", out)
    return out + ("…" if len(value) > 40 else "")


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y")


def _age(value: str, today: date) -> int | None:
    v = value.strip()[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value) else value.strip()
    for fmt in _DATE_FORMATS:
        try:
            born = datetime.strptime(v, fmt).date()
        except ValueError:
            continue
        if born > today:
            return None
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return None


def _minors(kind: str, values: list[str], today: date) -> tuple[int, int]:
    """(values that parse as an age or birth date, of which under 18)."""
    parsed = minors = 0
    for v in values:
        if kind == "AGE":
            age = int(v) if v.isdigit() and int(v) < 130 else None
        else:
            age = _age(v, today)
        if age is not None:
            parsed += 1
            minors += age < 18
    return parsed, minors


# Kinds only a column name can reveal (values are just words or numbers).
NAME_ONLY = {
    "AGE",
    "GENDER",
    "DATE_OF_BIRTH",
    "PIN_CODE",
    "BANK_ACCOUNT",
    "FINANCIAL",
    "HEALTH",
    "BIOMETRIC",
    "DEVICE_ID",
    "GEOLOCATION",
    "ADDRESS",
    "CITY",
}
# Kinds Presidio finds by language-model guesswork rather than a pattern. On short codes
# ("OPD-1", "A+") these guesses misfire, so they need more agreement and never override
# what the column name says.
NER_KINDS = {"PERSON", "ADDRESS", "NRP"}


def analyse_column(
    column: str, values: list[str], engine: Engine, today: date | None = None
) -> ColumnResult | None:
    today = today or date.today()
    hint = hint_for(column)
    counts: dict[str, int] = {}
    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    for v in values:
        for kind, score in engine.detect(v).items():
            counts[kind] = counts.get(kind, 0) + 1
            scores[kind] = max(scores.get(kind, 0.0), score)
            hits.setdefault(kind, []).append(v)
    n = len(values)
    ratios = {k: c / n for k, c in counts.items()} if n else {}
    strong = {k: r for k, r in ratios.items() if r >= (0.5 if k in NER_KINDS else VALUE_THRESHOLD)}
    if hint:
        strong = {k: r for k, r in strong.items() if k not in NER_KINDS or k == hint}
    prose = n and sum(" " in v and len(v) >= 15 for v in values) / n >= 0.3

    if hint and hint in ratios:
        primary, confidence = hint, "high"
    elif strong:
        primary = max(strong, key=lambda k: (strong[k], scores[k]))
        confidence = "high" if strong[primary] >= 0.8 and scores[primary] >= 0.8 else "medium"
        if hint and hint not in NAME_ONLY and primary != hint:
            confidence = "medium"
    elif hint:
        # A telling column name with nothing in the values to contradict it. Without
        # Presidio's name recognition, a name column is also only known by its name.
        name_only = NAME_ONLY | ({"PERSON"} if engine.name == "builtin" else set())
        primary = hint
        confidence = "medium" if hint in name_only or not n else "low"
    elif prose and ratios:
        # Free text that sometimes contains personal data (notes, comments).
        primary = max(ratios, key=lambda k: (ratios[k], -RISK_ORDER[KINDS[k].risk]))
        if ratios[primary] < 0.05:
            return None
        confidence = "low"
    else:
        return None

    entities = {
        k: round(r, 2)
        for k, r in sorted(ratios.items(), key=lambda kv: -kv[1])
        if r >= 0.1 or k == primary
    }
    if primary not in entities:
        entities = {primary: 0.0, **entities}
    minors = 0
    if primary in ("DATE_OF_BIRTH", "AGE"):
        parsed, minors = _minors(primary, values, today)
        if n and parsed / n < 0.5 and not hint:
            return None
    kind = KINDS[primary]
    source = hits.get(primary) or values
    shapes = list(dict.fromkeys(shape(v) for v in source[:20]))[:2]
    return ColumnResult(
        column=column,
        entities=entities,
        primary=primary,
        category="Children" if minors else kind.category,
        risk="high" if minors else kind.risk,
        confidence=confidence,
        match_ratio=round(ratios.get(primary, 0.0), 2),
        sampled=n,
        shapes=shapes,
        minors=minors,
        name_hint=hint,
    )


def scan_table(
    table: Table,
    engine: Engine,
    progress: Callable[[int, int], None] | None = None,
    today: date | None = None,
) -> list[ColumnResult]:
    results = []
    for i, column in enumerate(table.columns, start=1):
        r = analyse_column(column, table.samples[column], engine, today)
        if r:
            results.append(r)
        if progress:
            progress(i, len(table.columns))
    results.sort(key=lambda r: (RISK_ORDER[r.risk], -r.match_ratio, r.column))
    return results
