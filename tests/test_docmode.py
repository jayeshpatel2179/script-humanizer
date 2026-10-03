import asyncio
import re
from pathlib import Path

import pytest

from bot import docflow, emotion, gdoc
from bot.docflow import GateFailure, check_structure
from bot.docmode import GO_HUMANIZE_RE
from bot.llm import EditResult
from bot.pipeline import HumanizeResult
from bot.scan import analyze

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8").strip()


# --- command matching ----------------------------------------------------------


@pytest.mark.parametrize("text", ["go humanize", "Go Humanize", "  go  humanise!  ", "GO HUMANIZE."])
def test_go_humanize_matches(text):
    assert GO_HUMANIZE_RE.match(text)


@pytest.mark.parametrize("text", ["go humanize this", "please go humanize", "go", "humanize", "go humanize now"])
def test_go_humanize_rejects_other_text(text):
    assert not GO_HUMANIZE_RE.match(text)


# --- genuine strip -----------------------------------------------------------


@pytest.mark.parametrize("before, after", [
    ("He is a genuinely good passer.", "He is a good passer."),
    ("It was a genuinely awful night.", "It was an awful night."),
    ("A genuine idea.", "An idea."),
    ("Genuinely, this matters.", "This matters."),
    ("That is genuinely ironic, and GENUINE fun.", "That is ironic, and fun."),
])
def test_strip_genuine(before, after):
    text, count, _ = emotion.strip_genuine(before)
    assert text == after
    assert count == len(re.findall(r"genuine", before, re.IGNORECASE))


def test_strip_genuine_flags_predicate_use():
    text, count, flags = emotion.strip_genuine("The passion is genuine. Next line.")
    assert "genuine" not in text.lower() and count == 1
    assert flags and "passion is genuine" in flags[0]


def test_strip_genuine_on_samples_leaves_none():
    for name in ("real_madrid_inter.txt", "fc27_review.txt"):
        text, count, _ = emotion.strip_genuine(_load(name))
        assert count > 0
        assert not emotion.GENUINE_RE.search(text)


# --- cues ----------------------------------------------------------------------


def test_cue_pattern_and_strip():
    pattern = emotion.cue_pattern("brackets")
    text = "[thoughtful] This matters. [Editor note: cut here] [very excited] Wow. [whispering, fearful] Hm."
    assert pattern.findall(text) == ["[thoughtful]", "[very excited]", "[whispering, fearful]"]
    assert emotion.strip_cues(text, "brackets") == "This matters. [Editor note: cut here] Wow. Hm."
    assert emotion.cue_pattern("none") is None


def test_tidy_cues(monkeypatch):
    monkeypatch.setattr(emotion.config, "CUE_STYLE", "brackets")
    assert emotion._tidy_cues("[Thoughtful] Hi [VAR] check.", "Hi [VAR] check.") == "[thoughtful] Hi [VAR] check."
    assert emotion._tidy_cues("[analytical]: This. [curious] — Why? [calm]:no", "") ==         "[analytical] This. [curious] Why? [calm]:no"


def test_system_prompt_keeps_verbatim_prompt_first():
    prompt = emotion.system_prompt("brackets")
    assert prompt.startswith(emotion.EMOTION_PROMPT.rstrip())
    assert "[thoughtful]" in prompt


# --- structure gate ------------------------------------------------------------


def test_structure_passes_on_unchanged_samples():
    for name in ("real_madrid_inter.txt", "fc27_review.txt"):
        text = _load(name)
        failures, _ = check_structure(text, text)
        assert failures == []


def test_structure_catches_changed_and_missing_headers():
    source = _load("real_madrid_inter.txt")
    changed = source.replace("Short Two. Hello editor", "Short Two. Hi editor")
    assert any("Short 2 header line was changed" in f for f in check_structure(source, changed)[0])
    missing = re.sub(r"(?m)^Short Two\..*$", "", source)
    assert any("Short 2 header is missing" in f for f in check_structure(source, missing)[0])


def test_structure_catches_missing_outro_and_added_chapter():
    source = _load("real_madrid_inter.txt")
    output = source.replace("If you liked watching this", "Thanks for watching") + "\nChapter 1\n"
    failures, _ = check_structure(source, output)
    assert any("Outro" in f for f in failures)
    assert any("Chapter" in f for f in failures)


# --- the two steps with a fake model ---------------------------------------------


def _fake_humanize(script: str) -> HumanizeResult:
    text = re.sub(r"\bgenuinely ", "", script)
    text = re.sub(r"(?m)^Let's talk about ", "Now for ", text)
    return HumanizeResult(text=text, report="", truncated=False)


def _cue_every_third_paragraph(text: str, system_prompt: str = "") -> EditResult:
    # Mixed-case cue to check it gets normalised; placeholders untouched.
    paras = text.split("\n\n")
    out = [f"[Thoughtful] {p}" if i % 3 == 0 and not p.startswith("@@") else p for i, p in enumerate(paras)]
    return EditResult(text="\n\n".join(out), truncated=False)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def brackets(monkeypatch):
    monkeypatch.setattr(emotion.config, "CUE_STYLE", "brackets")


def test_run_humanize_adds_no_cues_and_strips_genuine(monkeypatch, brackets):
    calls = {"n": 0}

    async def humanize(script):
        calls["n"] += 1
        return _fake_humanize(script)

    monkeypatch.setattr(docflow, "humanize", humanize)
    outcome = _run(docflow.run_humanize(_load("real_madrid_inter.txt")))

    assert calls["n"] == 1
    assert not emotion.GENUINE_RE.search(outcome.text)
    assert docflow.cue_sequence(outcome.text) == []
    report = "\n".join(outcome.report_lines)
    assert "<b>genuine(ly)</b> x27" in report
    assert "\"let's talk about\" x13 → varied" in report
    assert "🔎 <b>Checks:</b> intro ✅ outro ✅ Short 1 ✅ Short 2 ✅ names/numbers ✅" in report
    assert "Emotion cues" not in report and "Existing cues kept" not in report


def test_run_humanize_reports_existing_cues(monkeypatch, brackets):
    async def humanize(script):
        return HumanizeResult(text=script, report="")

    monkeypatch.setattr(docflow, "humanize", humanize)
    source = _load("real_madrid_inter.txt").replace("Porto host", "[curious] Porto host", 1)
    outcome = _run(docflow.run_humanize(source))
    assert "🎭 <b>Existing cues kept:</b> 1" in "\n".join(outcome.report_lines)


def test_truncated_humanize_fails_after_one_retry(monkeypatch):
    calls = {"n": 0}

    async def humanize(script):
        calls["n"] += 1
        return HumanizeResult(text=script, report="", truncated=True)

    monkeypatch.setattr(docflow, "humanize", humanize)
    with pytest.raises(GateFailure) as info:
        _run(docflow.run_humanize(_load("fc27_review.txt")))
    assert calls["n"] == 2 and info.value.stage == "Go Humanize"
    assert any("cut off" in f for f in info.value.failures)


def _humanized(name: str) -> str:
    return emotion.strip_genuine(_fake_humanize(_load(name)).text)[0]


def test_run_emotion_only_adds_cues(monkeypatch, brackets):
    async def edit_script(text, system_prompt):
        return _cue_every_third_paragraph(text)

    monkeypatch.setattr(emotion, "edit_script", edit_script)
    source = _humanized("real_madrid_inter.txt")
    outcome = _run(docflow.run_emotion(source))

    assert docflow.normalise_ws(emotion.strip_cues(outcome.text)) == docflow.normalise_ws(source)
    cues = docflow.cue_sequence(outcome.text)
    assert cues and set(cues) == {"[thoughtful]"}
    for header in ("[Editor note:", "Short One. Hello editor", "Short Two. Hello editor", "Difficult to pronounce"):
        assert header in outcome.text
    report = "\n".join(outcome.report_lines)
    assert f"🎭 <b>Cues added:</b> {len(cues)} (thoughtful {len(cues)})" in report
    assert "script text unchanged ✅ Short headers untouched ✅ no cues on editor notes ✅" in report


@pytest.mark.parametrize("damage, expected", [
    (lambda t: t.replace("Porto host", "Porto welcome", 1), "Script text changed"),
    # The model only sees placeholders for headers; a cue in front of one lands on the restored header line.
    (lambda t: t.replace("@@KEEP_3@@", "[calm] @@KEEP_3@@", 1), "Short headers"),
    (lambda t: t.replace("Porto host", "(excited) Porto host", 1), "Script text changed"),
    (lambda t: t.replace("Porto host", "[Excited!] Porto host", 1), "Cue not in the [emotion] format"),
    (lambda t: t.replace("[Thoughtful] ", ""), "No emotion cues were added"),
])
def test_emotion_gate_catches(monkeypatch, brackets, damage, expected):
    calls = {"n": 0}

    async def edit_script(text, system_prompt):
        calls["n"] += 1
        return EditResult(text=damage(_cue_every_third_paragraph(text).text), truncated=False)

    monkeypatch.setattr(emotion, "edit_script", edit_script)
    with pytest.raises(GateFailure) as info:
        _run(docflow.run_emotion(_humanized("real_madrid_inter.txt")))
    assert calls["n"] == 2 and info.value.stage == "Add Emotion"
    assert any(expected in f for f in info.value.failures), info.value.failures


def test_repeats_count_digits_and_words_alike(brackets):
    before = ("After two hundred hours, it holds up.\n\nAfter two hundred hours, the gap is big.\n\n"
              "Two hundred hours in, it still works. The two-hundred hours show.")
    after = ("After 200 hours, it holds up.\n\nBy now the gap is big.\n\n"
             "At 200 hours in, it still works. The 200-hour mark shows.")
    items = docflow._repeat_items(analyze(before), before, after)
    assert '"after two hundred" x2 → varied' in items  # only one paragraph still opens that way
    assert '"two hundred hours" x4 → 2' in items  # not "→ 0": "200 hours" is the same phrase
    assert docflow._count_phrase("a 200-plus run, two-hundred plus", "two hundred plus") == 2


def test_report_escapes_html(monkeypatch, brackets):
    source = _load("fc27_review.txt")
    report = "\n".join(docflow.humanize_report(source, source, ['a <genuine> & "odd" line']))
    assert "<genuine>" not in report
    assert "a &lt;genuine&gt; &amp; \"odd\" line" in report
    # Only our own tags remain once escaped text is removed.
    assert set(re.findall(r"</?(\w+)", report)) == {"b"}


def test_by_section_escapes_titles(monkeypatch, brackets):
    body = ("Intro line here.\n\nR&D <Plans>\n\n[curious] Text one.\n\nThe Next Bit\n\n[calm] Text two.")
    line = docflow._by_section(emotion.strip_cues(body), body)
    assert line == "🗂 <b>By section:</b> Intro: 0; R&amp;D &lt;Plans&gt;: 1; The Next Bit: 1"


# --- Google Docs request shape (no network) ------------------------------------


class _FakeDocs:
    def __init__(self):
        self.body = None

    def documents(self):
        return self

    def batchUpdate(self, documentId, body):
        self.body = body
        return self

    def execute(self, **_):
        return {}


def test_replace_request_keeps_final_newline_and_locks_revision(monkeypatch):
    fake = _FakeDocs()
    monkeypatch.setattr(gdoc, "_get_service", lambda: fake)
    snapshot = gdoc.DocSnapshot(text="old", revision_id="rev-1", end_index=50)
    gdoc._replace_sync("doc", "t.0", "New 😀 text\n", snapshot)

    requests = fake.body["requests"]
    assert fake.body["writeControl"] == {"requiredRevisionId": "rev-1"}
    assert requests[0]["deleteContentRange"]["range"] == {"startIndex": 1, "endIndex": 49, "tabId": "t.0"}
    assert requests[1]["insertText"] == {"location": {"index": 1, "tabId": "t.0"}, "text": "New 😀 text"}
    # "New 😀 text" is 11 UTF-16 units (the emoji counts as 2).
    assert requests[2]["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 12, "tabId": "t.0"}
    # Every request targets the tab explicitly.
    for request in requests:
        (body,) = request.values()
        assert body.get("range", body.get("location"))["tabId"] == "t.0"


def test_replace_on_empty_doc_skips_delete(monkeypatch):
    fake = _FakeDocs()
    monkeypatch.setattr(gdoc, "_get_service", lambda: fake)
    gdoc._replace_sync("doc", "t.0", "Hello", gdoc.DocSnapshot(text="", revision_id="r", end_index=2))
    assert "deleteContentRange" not in fake.body["requests"][0]


def test_extract_and_normalise():
    content = [
        {"paragraph": {"elements": [{"textRun": {"content": "Line one\u000bstill one  \n"}}]}},
        {"table": {"tableRows": [{"tableCells": [{"content": [
            {"paragraph": {"elements": [{"textRun": {"content": "cell\n"}}]}}]}]}]}},
        {"paragraph": {"elements": [{"textRun": {"content": "\n"}}]}},
    ]
    assert gdoc.normalise(gdoc._extract(content)) == "Line one\nstill one\ncell"
