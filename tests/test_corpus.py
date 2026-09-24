import json
import shutil
from pathlib import Path

import pytest

from grc_agent.corpus import ingest, load_corpus
from grc_agent.corpus.cli import main
from grc_agent.corpus.ingest import parse_source
from grc_agent.corpus.store import evaluate, tokenize

FIXTURE = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def built(tmp_path):
    corpus_dir = tmp_path / "corpus"
    shutil.copytree(FIXTURE, corpus_dir)
    report = ingest(corpus_dir)
    return corpus_dir, report, load_corpus(corpus_dir / "build")


def refs(corpus):
    return [c.ref for c in corpus.chunks]


def test_ingest_report_is_clean(built):
    _, report, _ = built
    assert report.ok
    act, rules = report.sources
    assert (act.units, act.first, act.last) == (9, "Section 1(1)", "Section 9")
    assert (rules.units, rules.first, rules.last) == (4, "Rule 1(1)", "Rule 4(2)")


def test_chunks_are_cut_at_subsection_level(built):
    _, _, corpus = built
    assert refs(corpus)[:4] == ["Section 1(1)", "Section 1(2)", "Section 2", "Section 3(1)"]
    assert "Section 5(3)" in refs(corpus) and "Section 7" in refs(corpus)


def test_every_chunk_resolves_and_clauses_are_indexed(built):
    _, _, corpus = built
    assert all(corpus.index.resolves(r) for r in refs(corpus))
    assert corpus.index.resolves("Section 5(3)(b)")
    assert corpus.index.resolves("Rule 3(c)")
    assert not corpus.index.resolves("Section 5(3)(i)")  # a sub-clause, not a clause
    assert not corpus.index.resolves("Section 10")


def test_headings_marginal_and_inline(built):
    _, _, corpus = built
    assert corpus.provision("Section 6(1)")[0].heading == "Processing of personal data of children"
    assert corpus.provision("Rule 4(2)")[0].heading == "Intimation of personal data breach"


def test_page_noise_and_stray_numbers_are_ignored(built):
    _, _, corpus = built
    text = " ".join(c.text for c in corpus.chunks)
    assert "GAZETTE" not in text and "PART II" not in text
    assert "The period of 12 months in rule 4" in corpus.provision("Section 5(3)")[0].text


def test_schedules(built):
    _, _, corpus = built
    assert "two hundred and fifty crore" in corpus.provision("Schedule")[0].text
    assert "Consent Manager" in corpus.provision("First Schedule")[0].text
    assert corpus.provision("Schedule 2")


def test_provision_lookup_parents_and_clauses(built):
    _, _, corpus = built
    assert [c.ref for c in corpus.provision("Section 5")] == [
        "Section 5(1)",
        "Section 5(2)",
        "Section 5(3)",
    ]
    assert corpus.provision("Sec. 5 (3)(a)")[0].ref == "Section 5(3)"
    assert corpus.provision("Section 77") == []


def test_retrieval_meets_h3_bar_on_fixture(built):
    _, _, corpus = built
    result = evaluate(corpus, json.loads((FIXTURE / "questions.json").read_text()))
    assert result["hits"] >= 9, [r for r in result["rows"] if not r["hit"]]


def test_missing_sections_are_reported():
    pages = [
        "1. First section text here.\n2. Second section text here.\n4. Fourth section text here."
    ]
    _, _, report = parse_source("act", "gap.txt", pages)
    assert report.missing == ["Section 3"]


def test_out_of_sequence_numbers_do_not_start_sections():
    pages = ["1. First section says the fee is\n25. rupees per page.\n2. Second section."]
    chunks, _, _ = parse_source("act", "x.txt", pages)
    assert [c.ref for c in chunks] == ["Section 1", "Section 2"]
    assert "25. rupees" in chunks[0].text


def test_pdf_sources(tmp_path):
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _write_pdf(
        corpus_dir / "act.pdf",
        [
            [
                "THE SAMPLE ACT",
                "Short title.",
                "1. (1) This Act may be called the Sample Act.",
                "(2) It extends to the whole of India.",
            ],
            ["2", "Duty to protect.", "2. A person shall protect personal data from a breach."],
        ],
    )
    (corpus_dir / "manifest.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "file": "act.pdf",
                        "kind": "act",
                        "title": "t",
                        "source_url": "u",
                        "downloaded": "d",
                    }
                ]
            }
        )
    )
    report = ingest(corpus_dir)
    corpus = load_corpus(corpus_dir / "build")
    assert report.ok
    assert refs(corpus) == ["Section 1(1)", "Section 1(2)", "Section 2"]
    assert corpus.provision("Section 2")[0].page == 2


def test_manifest_errors(tmp_path):
    with pytest.raises(FileNotFoundError, match="manifest.json"):
        ingest(tmp_path)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"sources": [{"file": "a.txt", "kind": "act"}]})
    )
    with pytest.raises(ValueError, match="title"):
        ingest(tmp_path)


def test_tokenize_stems_plurals():
    assert tokenize("Breaches of the obligations") == tokenize("breach obligation")


def test_cli(built, capsys):
    corpus_dir, _, _ = built
    args = ["--corpus-dir", str(corpus_dir)]
    assert main([*args, "ingest"]) == 0
    assert "OK" in capsys.readouterr().out
    assert main([*args, "search", "breach intimation to the Board"]) == 0
    assert "Section 5(2)" in capsys.readouterr().out
    assert main([*args, "show", "Section 6(2)"]) == 0
    assert "targeted advertising" in capsys.readouterr().out
    assert main([*args, "show", "Section 99"]) == 1
    assert main([*args, "eval", str(FIXTURE / "questions.json")]) == 0
    assert "Meets the H3 bar" in capsys.readouterr().out


def test_cli_without_build(tmp_path, capsys):
    assert main(["--corpus-dir", str(tmp_path), "search", "x"]) == 2
    assert "no corpus built" in capsys.readouterr().err


def _write_pdf(path: Path, pages: list[list[str]]) -> None:
    """Minimal text PDF, one line per string, so extraction is tested without extra libraries."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        None,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for lines in pages:
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for line in lines:
            ops.append(
                "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*"
            )
        ops.append("ET")
        stream = "\n".join(ops)
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = "%PDF-1.4\n", []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out.encode("latin-1")))
        out += f"{i} 0 obj\n{obj}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))
