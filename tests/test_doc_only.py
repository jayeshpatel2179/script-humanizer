"""Doc-only flow: pre-flight checks, single edited message, no files, persistent
hashes and backups, legacy handlers behind a flag, cue-sequence checks."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import config, docflow, docmode, emotion, gdoc, intake, state
from bot.docflow import DocOutcome, GateFailure, check_emotion, check_humanize
from bot.emotion import EmotionResult
from bot.main import help_text
from bot.pipeline import HumanizeResult

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = (FIXTURES / "fc27_review.txt").read_text(encoding="utf-8").strip()


# --- fakes -------------------------------------------------------------------


class FakeMessage:
    def __init__(self, log: list):
        self.log = log
        self.chat_id = 1

    async def reply_text(self, text, **_):
        status = FakeMessage(self.log)
        self.log.append(("reply", text))
        return status

    async def edit_text(self, text, **_):
        self.log.append(("edit", text))

    async def reply_document(self, *args, **kwargs):
        raise AssertionError("Doc mode must never send a file")


class FakeBot:
    async def send_chat_action(self, **_):
        pass


def _update(log):
    return SimpleNamespace(effective_message=FakeMessage(log), effective_user=SimpleNamespace(id=1))


def _context():
    return SimpleNamespace(chat_data={}, bot=FakeBot())


@pytest.fixture
def doc(monkeypatch, tmp_path):
    """A fake Doc + isolated state dir. Returns a dict to set the Doc text and inspect writes."""
    fake = {"text": SCRIPT, "writes": []}

    async def read_doc():
        return gdoc.DocSnapshot(text=gdoc.normalise(fake["text"]), revision_id="rev", end_index=len(fake["text"]) + 2)

    async def replace_doc(new_text, snapshot):
        fake["writes"].append(new_text)
        fake["text"] = new_text

    async def process_script(script):
        return DocOutcome(text="[thoughtful] " + script + "\n", report_lines=["Words: 1 → 1"], warnings=[])

    monkeypatch.setattr(gdoc, "read_doc", read_doc)
    monkeypatch.setattr(gdoc, "replace_doc", replace_doc)
    monkeypatch.setattr(docmode, "process_script", process_script)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "GOOGLE_DOC_ID", "doc123")
    monkeypatch.setattr(config, "DOC_WRITE_ENABLED", True)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", frozenset())
    return fake


def _go(log=None):
    log = [] if log is None else log
    asyncio.run(docmode.on_go_humanize(_update(log), _context()))
    return log


# --- pre-flight ----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\n  \t "])
def test_empty_or_whitespace_doc(doc, text):
    doc["text"] = text
    assert _go() == [("reply", "The Doc is empty. Paste the script into the Doc, then send: go humanize")]
    assert doc["writes"] == []


def test_short_script(doc):
    doc["text"] = "Just a few words here, not a script."
    log = _go()
    assert log == [("reply", "That's only 8 words, too short to be a script. "
                             "Paste the full script into the Doc, then send: go humanize")]


def test_success_is_one_edited_message_and_writes_doc(doc):
    log = _go()
    assert [kind for kind, _ in log] == ["reply", "edit"]
    assert log[0][1].startswith("Processing 1,827 words")
    report = log[1][1]
    assert report.startswith("Script humanized\nDoc: https://docs.google.com/document/d/doc123/edit?tab=t.0")
    assert len(doc["writes"]) == 1


def test_same_script_by_output_hash_then_input_hash_survives_restart(doc):
    _go()  # processes and writes
    assert _go()[-1][1].startswith("This is the same script I already processed.")  # Doc holds our output

    doc["text"] = SCRIPT  # paste the original raw script again
    state_file = Path(config.STATE_DIR) / "state.json"
    assert state_file.exists()  # hashes live on disk, not in memory
    assert _go()[-1][1].startswith("This is the same script I already processed.")
    assert len(doc["writes"]) == 1


def test_hash_ignores_whitespace_and_line_endings():
    assert state.text_hash("A  b\r\n\r\nc ") == state.text_hash("A b\n\nc")


def test_lock_rejects_second_command(doc):
    async def both():
        log = []
        await docmode._job_lock.acquire()
        try:
            await docmode.on_go_humanize(_update(log), _context())
        finally:
            docmode._job_lock.release()
        return log

    assert asyncio.run(both()) == [("reply", "Already working on a script. I'll send the report when it's done.")]


def test_gate_failure_leaves_doc_untouched(doc, monkeypatch):
    async def failing(script):
        raise GateFailure("Humanize pass", ["Short 2 header is missing"])

    monkeypatch.setattr(docmode, "process_script", failing)
    log = _go()
    assert log[-1] == ("edit", "Couldn't finish: Humanize pass check failed twice - Short 2 header is missing. "
                               "The Doc was not changed.")
    assert doc["writes"] == []


def test_google_write_failure_leaves_doc_untouched(doc, monkeypatch):
    async def broken(new_text, snapshot):
        raise gdoc.DocError("Google Docs error (500): backend error")

    monkeypatch.setattr(gdoc, "replace_doc", broken)
    log = _go()
    assert log[-1][1].startswith("Couldn't finish: writing the Doc failed - Google Docs error (500)")
    assert log[-1][1].endswith("The Doc was not changed.")
    assert not (Path(config.STATE_DIR) / "state.json").exists()  # nothing recorded as processed


def test_dry_run_sends_no_file_and_does_not_write(doc, monkeypatch):
    monkeypatch.setattr(config, "DOC_WRITE_ENABLED", False)
    log = _go()
    assert log[-1][1].startswith("Dry run - the Doc was NOT changed.")
    assert doc["writes"] == []


def test_backups_keep_last_three(doc, monkeypatch, tmp_path):
    for i in range(5):
        state.save_backup(f"script {i}")
    backups = sorted((tmp_path / "backups").glob("backup_*.txt"))
    assert [b.read_text(encoding="utf-8") for b in backups] == ["script 2", "script 3", "script 4"]


# --- help + legacy flag -----------------------------------------------------------


def test_help_text(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_DOC_ID", "doc123")
    assert help_text() == (
        "I clean up finished video scripts so they sound human.\n\n"
        "1. Paste the script into the Google Doc: https://docs.google.com/document/d/doc123/edit?tab=t.0\n"
        "2. Send me: go humanize\n\n"
        "I replace the script in the Doc with the cleaned version and send you the link with a short report.\n\n"
        "I remove repeated and filler words, vary repeated openers, rewrite \"it's not X, it's Y\" lines and "
        "simplify hard words. Emotion cues, editor notes, short headers and the pronunciation list stay as they are."
    )


@pytest.mark.parametrize("handler", [intake.on_document, intake.on_go, intake.on_cancel, intake.on_text])
def test_legacy_handlers_point_to_doc(monkeypatch, handler):
    monkeypatch.setattr(config, "LEGACY_TXT_FLOW", False)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", frozenset())
    log = []
    asyncio.run(handler(_update(log), _context()))
    assert log == [("reply", intake.DOC_POINTER)]


def test_pasted_chunks_get_one_pointer(monkeypatch):
    monkeypatch.setattr(config, "LEGACY_TXT_FLOW", False)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", frozenset())
    log, context = [], _context()
    for _ in range(4):
        asyncio.run(intake.on_text(_update(log), context))
    assert log == [("reply", intake.DOC_POINTER)]


# --- cue-sequence checks ---------------------------------------------------


def _with_cues(text: str) -> str:
    paras = text.split("\n\n")
    return "\n\n".join(f"[curious] {p}" if i in (2, 5) else f"[analytical] {p}" if i == 3 else p
                       for i, p in enumerate(paras))


def test_humanize_must_keep_existing_cue_sequence(monkeypatch):
    monkeypatch.setattr(config, "CUE_STYLE", "brackets")
    source = _with_cues(SCRIPT)
    kept = HumanizeResult(text=source, report="")
    dropped = HumanizeResult(text=source.replace("[analytical] ", "", 1), report="")
    swapped = HumanizeResult(text=source.replace("[curious]", "[X]", 1).replace("[analytical]", "[curious]", 1)
                             .replace("[X]", "[analytical]", 1), report="")
    assert not any("cues" in f for f in check_humanize(source, kept))
    assert any("cues" in f for f in check_humanize(source, dropped))
    assert any("cues" in f for f in check_humanize(source, swapped))


def test_emotion_may_add_cues_but_not_drop_existing(monkeypatch):
    monkeypatch.setattr(config, "CUE_STYLE", "brackets")
    source = _with_cues(SCRIPT)
    added = EmotionResult(text="[thoughtful] " + emotion.strip_genuine(source)[0])
    dropped = EmotionResult(text=emotion.strip_genuine(source)[0].replace("[curious] ", ""))
    assert not any("cues" in f for f in check_emotion(source, source, added))
    assert any("cues" in f for f in check_emotion(source, source, dropped))


def test_dropped_cue_retries_humanize_then_fails(monkeypatch):
    monkeypatch.setattr(config, "CUE_STYLE", "brackets")
    source = _with_cues(SCRIPT)
    calls = {"n": 0}

    async def humanize(script):
        calls["n"] += 1
        return HumanizeResult(text=script.replace("[curious] ", ""), report="")

    monkeypatch.setattr(docflow, "humanize", humanize)
    with pytest.raises(GateFailure) as info:
        asyncio.run(docflow.process_script(source))
    assert calls["n"] == 2 and info.value.stage == "Humanize pass"
