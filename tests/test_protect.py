from pathlib import Path

from bot.protect import PLACEHOLDER, protect, restore, strip_placeholders

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8").strip()


def test_real_madrid_protects_note_shorts_and_pronunciation_list():
    protected = protect(_load("real_madrid_inter.txt"))
    blocks = list(protected.blocks.values())
    assert len(blocks) == 4
    assert blocks[0].startswith("[Editor note:")
    assert blocks[1].startswith("Short One. Hello editor")
    assert blocks[2].startswith("Short Two. Hello editor")
    assert blocks[3].startswith("Difficult to pronounce words")
    assert "Editor note" not in protected.text
    assert "Hello editor" not in protected.text
    assert "Dimarco, Carlos Augusto" not in protected.text  # only in the pronunciation list


def test_fc27_protects_short_headers_without_hello():
    blocks = list(protect(_load("fc27_review.txt")).blocks.values())
    assert [b.split(",")[0] for b in blocks] == ["Short 1", "Short 2"]


def test_unedited_round_trip_is_identical():
    for name in ("real_madrid_inter.txt", "fc27_review.txt"):
        original = _load(name)
        protected = protect(original)
        restored = restore(protected.text, protected)
        assert restored.text == original
        assert restored.reinserted == [] and restored.duplicated == []


def test_multiline_editor_note_is_one_block():
    text = "Intro line.\n[Editor note: montage\nof the goals]\nBody text."
    protected = protect(text)
    assert protected.blocks == {1: "[Editor note: montage\nof the goals]"}
    assert protected.text == f"Intro line.\n{PLACEHOLDER.format(n=1)}\nBody text."


def test_unclosed_bracket_is_not_protected():
    text = "[not a note\nline two\nline three"
    assert protect(text).blocks == {}


def test_dropped_placeholder_is_reinserted_after_previous_one():
    protected = protect("A.\n[note one]\nB.\n[note two]\nC.")
    edited = "A.\n@@KEEP_1@@\nB.\nC."  # model dropped @@KEEP_2@@
    restored = restore(edited, protected)
    assert restored.reinserted == [2]
    assert restored.text.index("[note one]") < restored.text.index("[note two]")


def test_tolerates_spaces_inside_placeholder():
    protected = protect("A.\n[note]\nB.")
    assert restore("A.\n@@ KEEP_1 @@\nB.", protected).text == "A.\n[note]\nB."


def test_strip_placeholders_removes_only_placeholder_lines():
    assert strip_placeholders("A.\n@@KEEP_1@@\nB.") == "A.\nB."
