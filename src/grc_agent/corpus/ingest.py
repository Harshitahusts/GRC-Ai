"""Corpus ingestion (ticket H2): Act and Rules text -> citable chunks.

Chunks are cut at sub-section level ("Section 5(1)") where a section has
sub-sections, otherwise at section level. Clauses such as "(a)" stay inside
their chunk but are added to the corpus index, so "Section 8(6)(a)" resolves.

The parser is deliberately strict: a new section is accepted only if it's the
next number in sequence, so a stray "12." inside running text can't start a
fake section. Gaps in numbering are reported, never silently skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from grc_agent.kpis.citations import CorpusIndex, normalize_citation

KINDS = {"act": "Section", "rules": "Rule"}

_NOISE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\d{1,4}$",  # page numbers
        r"^the gazette of india",
        r"^\[?part\s+ii",
        r"^sec\.\s*\d",
        r"^[_\-—=]{3,}$",
        r"^registered no\.",
        r"^(extraordinary|published by authority)$",
    )
]
_CHAPTER = re.compile(r"^chapter\s+[ivxlc]+\b", re.IGNORECASE)
_SECTION = re.compile(r"^(\d{1,3})([A-Z]?)\.\s*(.*)$")
_SUBSECTION = re.compile(r"^\((\d{1,3})\)\s*(.*)$")
_CLAUSE = re.compile(r"^\(([a-z]{1,2})\)\s")
_INLINE_HEADING = re.compile(r"^(.{3,160}?)\.\s*[—–-]{1,2}\s*(.*)$")
_SCHEDULE = re.compile(
    r"^(?:the\s+)?(?:(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+)?"
    r"schedule\b",
    re.IGNORECASE,
)
_ORDINAL = {
    w: i
    for i, w in enumerate(
        [
            "first",
            "second",
            "third",
            "fourth",
            "fifth",
            "sixth",
            "seventh",
            "eighth",
            "ninth",
            "tenth",
        ],
        start=1,
    )
}


@dataclass
class Chunk:
    ref: str  # display citation, e.g. "Section 5(1)"
    kind: str  # act | rules
    source: str  # manifest file name
    heading: str
    text: str
    page: int

    @property
    def key(self) -> str:
        return normalize_citation(self.ref) or self.ref.lower()


@dataclass
class SourceReport:
    file: str
    kind: str
    units: int = 0
    chunks: int = 0
    first: str = ""
    last: str = ""
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestReport:
    sources: list[SourceReport]
    chunks: int
    index_entries: int
    unresolved_chunks: list[str]

    @property
    def ok(self) -> bool:
        return (
            self.chunks > 0
            and not self.unresolved_chunks
            and all(s.units and not s.missing for s in self.sources)
        )


def _is_noise(line: str) -> bool:
    return any(p.match(line) for p in _NOISE)


def _looks_like_heading(line: str) -> bool:
    """A marginal note such as "Notice." printed just before a section."""
    return (
        len(line) <= 120
        and line.endswith(".")
        and line[:1].isupper()
        and not line.startswith("(")
        and not _SECTION.match(line)
    )


def extract_pages(path: Path) -> list[str]:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return [page.extract_text() or "" for page in PdfReader(path).pages]
    return path.read_text("utf-8").split("\f")


class _Parser:
    def __init__(self, kind: str, source: str, report: SourceReport) -> None:
        self.label = KINDS[kind]
        self.kind, self.source, self.report = kind, source, report
        self.chunks: list[Chunk] = []
        self.index: list[str] = []
        self.section: int | None = None
        self.section_suffix = ""
        self.subsection: int | None = None
        self.clause = ""
        self.heading = ""
        self.current: Chunk | None = None
        self.in_schedule = False
        self._pending_heading = ""

    # -- chunk management
    def _unit(self) -> str:
        base = f"{self.label} {self.section}{self.section_suffix}"
        return f"{base}({self.subsection})" if self.subsection else base

    def _open(self, ref: str, page: int, text: str) -> None:
        self._close()
        self.current = Chunk(ref, self.kind, self.source, self.heading, text.strip(), page)
        self.index.append(ref)

    def _close(self) -> None:
        if self.current and self.current.text:
            self.chunks.append(self.current)
        self.current = None

    def _append(self, line: str) -> None:
        if self.current is None:
            return
        text = self.current.text
        if text.endswith("-") and not text.endswith(" -"):
            self.current.text = text[:-1] + line  # re-join a hyphenated line break
        else:
            self.current.text = f"{text} {line}".strip()

    # -- line handling
    def feed(self, line: str, page: int) -> None:
        schedule = _SCHEDULE.match(line)
        if schedule and line.upper() == line:
            ordinal = schedule[1]
            ref = f"Schedule {_ORDINAL[ordinal.lower()]}" if ordinal else "Schedule"
            self.in_schedule, self.heading = True, ""
            self.subsection = None
            self._open(ref, page, "")
            return
        if self.in_schedule:
            self._append(line)
            return
        if _CHAPTER.match(line) or (line.isupper() and len(line) < 120 and not _CLAUSE.match(line)):
            return  # chapter numbers and titles

        section = _SECTION.match(line)
        if section and self._is_next_section(int(section[1]), section[2]):
            self._start_section(int(section[1]), section[2], section[3], page)
            return

        sub = _SUBSECTION.match(line)
        if sub and self.section is not None and int(sub[1]) == (self.subsection or 0) + 1:
            self.subsection = int(sub[1])
            self.clause = ""
            self._open(self._unit(), page, sub[2])
            self._clause(sub[2])
            return

        self._clause(line)
        self._append(line)

    def _clause(self, text: str) -> None:
        match = _CLAUSE.match(text)
        if not match or self.section is None:
            return
        expected = chr(ord(self.clause) + 1) if self.clause else "a"
        if match[1] == expected:  # anything else is a sub-clause like (ii)
            self.clause = match[1]
            self.index.append(f"{self._unit()}({match[1]})")

    def _is_next_section(self, number: int, suffix: str) -> bool:
        if self.section is None:
            return number == 1
        if suffix:  # inserted section, e.g. 10A after 10
            return number == self.section and suffix > self.section_suffix
        if number == self.section + 1:
            return True
        if self.section < number <= self.section + 3:  # tolerate a gap, but report it
            label = self.label
            self.report.missing += [f"{label} {n}" for n in range(self.section + 1, number)]
            return True
        return False

    def _start_section(self, number: int, suffix: str, rest: str, page: int) -> None:
        # Headings come either inline ("3. Notice.—The notice shall ...") or on
        # their own line just before the section (set by parse_source).
        heading = ""
        inline = _INLINE_HEADING.match(rest)
        if inline and not rest.startswith("("):
            heading, rest = inline[1].strip(), inline[2]
        elif self._pending_heading:
            heading = self._pending_heading
        self._pending_heading = ""
        self.section, self.section_suffix = number, suffix
        self.subsection, self.clause, self.heading = None, "", heading
        self.report.units += 1

        sub = _SUBSECTION.match(rest)
        if sub and sub[1] == "1":
            self.index.append(f"{self.label} {number}{suffix}")
            self.subsection = 1
            self._open(self._unit(), page, sub[2])
            self._clause(sub[2])
        else:
            self._open(self._unit(), page, rest)
            self._clause(rest)


def parse_source(
    kind: str, file_name: str, pages: list[str]
) -> tuple[list[Chunk], list[str], SourceReport]:
    if kind not in KINDS:
        raise ValueError(f"{file_name}: kind must be one of {', '.join(KINDS)}")
    report = SourceReport(file_name, kind)
    parser = _Parser(kind, file_name, report)
    for page_number, page in enumerate(pages, start=1):
        lines = [ln.strip() for ln in page.splitlines()]
        lines = [ln for ln in lines if ln and not _is_noise(ln)]
        for i, line in enumerate(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            # A marginal heading sits on its own line right before the section it names.
            if _looks_like_heading(line) and _SECTION.match(nxt) and parser.section is not None:
                number = int(_SECTION.match(nxt)[1])
                if number == parser.section + 1:
                    parser._pending_heading = line.rstrip(".")
                    continue
            if parser.section is None and not parser.in_schedule:
                # Skip the long title and enacting formula before section 1.
                first = _SECTION.match(line)
                if not (first and first[1] == "1") and not (
                    _SCHEDULE.match(line) and line.isupper()
                ):
                    if _looks_like_heading(line) and _SECTION.match(nxt):
                        parser._pending_heading = line.rstrip(".")
                    continue
            parser.feed(line, page_number)
    parser._close()

    report.chunks = len(parser.chunks)
    units = [c.ref for c in parser.chunks if not c.ref.startswith("Schedule")]
    if units:
        report.first, report.last = units[0], units[-1]
    if not report.units:
        report.warnings.append(f"No {KINDS[kind].lower()}s found. Is this the right file and kind?")
    empty = [c.ref for c in parser.chunks if len(c.text) < 20]
    if empty:
        report.warnings.append("Very short chunks (check extraction): " + ", ".join(empty[:10]))
    return parser.chunks, parser.index, report


def load_manifest(corpus_dir: Path) -> list[dict]:
    path = corpus_dir / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. List each source file with its kind (act or rules), "
            "title, source_url and downloaded date."
        )
    sources = json.loads(path.read_text("utf-8")).get("sources", [])
    if not sources:
        raise ValueError(f"{path} lists no sources")
    for s in sources:
        for key in ("file", "kind", "title", "source_url", "downloaded"):
            if not s.get(key):
                raise ValueError(f"{path}: every source needs '{key}' ({s.get('file', '?')})")
        if not (corpus_dir / s["file"]).exists():
            raise FileNotFoundError(
                f"{corpus_dir / s['file']} is listed in the manifest but missing"
            )
    return sources


def ingest(corpus_dir: str | Path, out_dir: str | Path | None = None) -> IngestReport:
    """Parse every source in the manifest and write chunks.jsonl, corpus_index.txt, report.json."""
    corpus_dir = Path(corpus_dir)
    out = Path(out_dir) if out_dir else corpus_dir / "build"
    sources = load_manifest(corpus_dir)

    all_chunks: list[Chunk] = []
    all_refs: list[str] = []
    reports = []
    for s in sources:
        pages = extract_pages(corpus_dir / s["file"])
        chunks, refs, report = parse_source(s["kind"], s["file"], pages)
        all_chunks += chunks
        all_refs += refs
        reports.append(report)

    keys = [c.key for c in all_chunks]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        reports[0].warnings.append("Duplicate references across sources: " + ", ".join(dupes[:10]))

    index = CorpusIndex(all_refs)
    unresolved = [c.ref for c in all_chunks if not index.resolves(c.ref)]
    report = IngestReport(reports, len(all_chunks), len(set(all_refs)), unresolved)

    out.mkdir(parents=True, exist_ok=True)
    with (out / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    header = "# Generated by grc-corpus ingest from the sources in manifest.json. Do not edit.\n"
    (out / "corpus_index.txt").write_text(header + "\n".join(dict.fromkeys(all_refs)) + "\n")
    (out / "report.json").write_text(
        json.dumps({**asdict(report), "ok": report.ok, "manifest": sources}, indent=2)
    )
    return report
