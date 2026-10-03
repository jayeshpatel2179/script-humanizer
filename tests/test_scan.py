from pathlib import Path

from bot.protect import protect, strip_placeholders
from bot.scan import analyze, build_report, compare, extract_facts

FIXTURES = Path(__file__).parent / "fixtures"


def _body(name: str) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return strip_placeholders(protect(text).text)


def test_counts_real_madrid_tics():
    stats = analyze(_body("real_madrid_inter.txt"))
    assert stats.fillers["genuine(ly)"] == 27
    assert stats.fillers["real (filler)"] == 14  # "Real Madrid" / "Real Betis" not counted
    assert stats.repeated_openers["let's talk about"] == 13
    assert len(stats.contrast) == 4


def test_counts_fc27_tics():
    stats = analyze(_body("fc27_review.txt"))
    assert stats.fillers["genuine(ly)"] == 17
    assert dict(stats.repeated_phrases)["two hundred hours"] >= 10


def test_contrast_patterns():
    hits = analyze(
        "It's not a bug, it's a feature. That is not a team. It is a machine. "
        "He doesn't just want to win, he wants to dominate. This isn't purely luck. "
        "People think Inter press high. It's not. Not just fast, but smart."
    ).contrast
    assert len(hits) == 6
    assert analyze("The keeper is not fast. He reads the game well.").contrast == []


def test_facts_include_names_and_numbers_not_sentence_starts():
    facts = extract_facts("Porto host Manchester City. They ranked ninth with 9 crosses and a four-three win.")
    assert {"Manchester", "City", "ninth", "9", "four", "three"} <= facts
    assert "Porto" not in facts  # sentence start - could be any word
    assert "They" not in facts


def test_missing_fact_is_flagged_and_number_forms_are_equivalent():
    before = "Real Madrid lost to Bayern Munich, a four-three defeat. Inter ranked ninth in Serie A."
    after = "Real Madrid lost 4-3. Inter ranked 9th in Serie A."
    result = compare(before, after, length_tolerance=1.0)
    assert result.facts_missing == ["Bayern", "Munich"]
    assert any("missing" in w for w in result.warnings)


def test_length_warning():
    result = compare("one two three four five six seven eight nine ten", "one two three", length_tolerance=0.10)
    assert any("Length changed" in w for w in result.warnings)


def test_report_mentions_key_sections():
    body = _body("real_madrid_inter.txt")
    result = compare(body, body, length_tolerance=0.10)
    report = build_report(result, protected_total=4, reinserted=[], duplicated=[], truncated=False)
    assert "genuine(ly) 27→27" in report
    assert "Protected lines: 4/4" in report
    assert "names/numbers kept" in report
