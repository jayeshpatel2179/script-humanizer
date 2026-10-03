import asyncio
import re
from pathlib import Path

import pytest

from bot import docflow, emotion, gdoc
from bot.docflow import GateFailure, check_structure, process_script
from bot.docmode import GO_HUMANIZE_RE
from bot.llm import EditResult
from bot.pipeline import HumanizeResult

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
    text = "[thoughtful] This matters. [Editor note: cut here] [very excited] Wow."
    assert pattern.findall(text) == ["[thoughtful]", "[very excited]"]
    assert emotion.strip_cues(text, "brackets") == "This matters. [Editor note: cut here] Wow."
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


# --- full flow with a fake model ---------------------------------------------


def _fake_humanize(script: str) -> HumanizeResult:
    text = re.sub(r"\bgenuinely ", "", script)
    text = re.sub(r"(?m)^Let's talk about ", "Now for ", text)
    return HumanizeResult(text=text, report="", truncated=False)


def _fake_emotion_llm(text: str, system_prompt: str) -> EditResult:
    # Insert a cue before every third paragraph, keep placeholders, leave one "genuine" for code to catch.
    paras = text.split("\n\n")
    out = [f"[Thoughtful] {p}" if i % 3 == 0 and not p.startswith("@@") else p for i, p in enumerate(paras)]
    return EditResult(text="\n\n".join(out), truncated=False)


def _run(coro):
    return asyncio.run(coro)


def test_process_script_end_to_end(monkeypatch):
    calls = {"humanize": 0, "emotion": 0}

    async def humanize(script):
        calls["humanize"] += 1
        return _fake_humanize(script)

    async def edit_script(text, system_prompt):
        calls["emotion"] += 1
        return _fake_emotion_llm(text, system_prompt)

    monkeypatch.setattr(docflow, "humanize", humanize)
    monkeypatch.setattr(emotion, "edit_script", edit_script)
    monkeypatch.setattr(emotion.config, "CUE_STYLE", "brackets")

    source = _load("real_madrid_inter.txt")
    outcome = _run(process_script(source))

    assert calls == {"humanize": 1, "emotion": 1}
    assert not emotion.GENUINE_RE.search(outcome.text)
    assert "[thoughtful]" in outcome.text and "[Thoughtful]" not in outcome.text
    for header in ("[Editor note:", "Short One. Hello editor", "Short Two. Hello editor", "Difficult to pronounce"):
        assert header in outcome.text
    report = "\n".join(outcome.report_lines)
    assert "genuine/genuinely: 27 → 0" in report
    assert "Structure: intro ✓ · outro ✓ · Short 1 ✓ · Short 2 ✓" in report
    assert "Emotion cues added:" in report


def test_emotion_rewrite_fails_gate_after_one_retry(monkeypatch):
    calls = {"emotion": 0}

    async def humanize(script):
        return _fake_humanize(script)

    async def edit_script(text, system_prompt):
        calls["emotion"] += 1
        # Rewrites the whole body - the similarity check must catch it.
        lines = text.split("\n\n")
        return EditResult(text="\n\n".join(p if p.startswith("@@") else "Totally different words here." for p in lines),
                          truncated=False)

    monkeypatch.setattr(docflow, "humanize", humanize)
    monkeypatch.setattr(emotion, "edit_script", edit_script)
    monkeypatch.setattr(emotion.config, "CUE_STYLE", "brackets")

    with pytest.raises(GateFailure) as info:
        _run(process_script(_load("real_madrid_inter.txt")))
    assert info.value.stage == "Emotion pass"
    assert calls["emotion"] == 2
    assert any("similarity" in f for f in info.value.failures)


def test_truncated_humanize_fails_gate(monkeypatch):
    async def humanize(script):
        return HumanizeResult(text=script, report="", truncated=True)

    monkeypatch.setattr(docflow, "humanize", humanize)
    with pytest.raises(GateFailure) as info:
        _run(process_script(_load("fc27_review.txt")))
    assert info.value.stage == "Humanize pass"
    assert any("cut off" in f for f in info.value.failures)


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
    gdoc._replace_sync("doc", "New 😀 text\n", snapshot)

    requests = fake.body["requests"]
    assert fake.body["writeControl"] == {"requiredRevisionId": "rev-1"}
    assert requests[0]["deleteContentRange"]["range"] == {"startIndex": 1, "endIndex": 49}
    assert requests[1]["insertText"] == {"location": {"index": 1}, "text": "New 😀 text"}
    # "New 😀 text" is 11 UTF-16 units (the emoji counts as 2).
    assert requests[2]["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 12}


def test_replace_on_empty_doc_skips_delete(monkeypatch):
    fake = _FakeDocs()
    monkeypatch.setattr(gdoc, "_get_service", lambda: fake)
    gdoc._replace_sync("doc", "Hello", gdoc.DocSnapshot(text="", revision_id="r", end_index=2))
    assert "deleteContentRange" not in fake.body["requests"][0]


def test_extract_and_normalise():
    content = [
        {"paragraph": {"elements": [{"textRun": {"content": "Line one\u000bstill one  \n"}}]}},
        {"table": {"tableRows": [{"tableCells": [{"content": [
            {"paragraph": {"elements": [{"textRun": {"content": "cell\n"}}]}}]}]}]}},
        {"paragraph": {"elements": [{"textRun": {"content": "\n"}}]}},
    ]
    assert gdoc.normalise(gdoc._extract(content)) == "Line one\nstill one\ncell"
