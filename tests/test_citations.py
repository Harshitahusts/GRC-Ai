import pytest

from grc_agent.kpis.citations import CorpusIndex, normalize_citation


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Section 5(1)", "section 5(1)"),
        ("section 5 (1)", "section 5(1)"),
        ("Sec. 8(6)(a)", "section 8(6)(a)"),
        ("S. 9", "section 9"),
        ("Rule 3(b)", "rule 3(b)"),
        ("R.7", "rule 7"),
        ("Schedule", "schedule"),
        ("Section 10A", "section 10a"),
        ("Second Schedule", "schedule 2"),
        ("the FIRST schedule", "schedule 1"),
        ("Schedule 2", "schedule 2"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_citation(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "Section", "Rule (1)", "Section 5(1) of the Act", "Article 5", "5(1)"]
)
def test_normalize_rejects_unparseable(raw):
    assert normalize_citation(raw) is None


def test_index_resolves_parents_but_not_children():
    index = CorpusIndex(["Section 8(6)(a)", "Rule 3"])
    assert index.resolves("Section 8(6)(a)")
    assert index.resolves("section 8 (6)")
    assert index.resolves("Section 8")
    assert not index.resolves("Section 8(6)(b)")
    assert not index.resolves("Rule 3(b)")
    assert not index.resolves("Section 5(1) of the Act")


def test_check_splits_resolved_and_unresolved():
    check = CorpusIndex(["Section 5(1)"]).check(["Section 5(1)", "Section 99"])
    assert check.resolved == ("Section 5(1)",)
    assert check.unresolved == ("Section 99",)
    assert not check.ok
    assert not CorpusIndex(["Section 5"]).check([]).ok


def test_index_rejects_bad_entries():
    with pytest.raises(ValueError):
        CorpusIndex(["not a citation"])


def test_index_from_file_skips_comments(tmp_path):
    path = tmp_path / "index.txt"
    path.write_text("# comment\n\nSection 5(1)\n")
    assert CorpusIndex.from_file(path).resolves("Section 5")
