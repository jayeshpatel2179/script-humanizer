"""3-button Doc flow: menu routing, Go Humanize, Add Emotion, Cancel, persistent
session state, pre-flight checks, one edited message per run, no files."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import config, docflow, docmode, gdoc, intake, state
from bot.docflow import GateFailure, StepOutcome
from bot.main import help_text, on_text_or_menu, start

FIXTURES = Path(__file__).parent / "fixtures"
FC27 = (FIXTURES / "fc27_review.txt").read_text(encoding="utf-8").strip()
FOOTBALL = (FIXTURES / "real_madrid_inter.txt").read_text(encoding="utf-8").strip()
LINK = "https://docs.google.com/document/d/doc123/edit?tab=t.0"


# --- fakes -------------------------------------------------------------------


class Chat:
    """Records everything the bot does in the chat."""

    def __init__(self):
        self.log: list[tuple] = []


class FakeMessage:
    def __init__(self, chat: Chat, text: str = ""):
        self.chat, self.text, self.chat_id = chat, text, 1
        self.markup = None

    async def reply_text(self, text, reply_markup=None, parse_mode=None, **_):
        self.chat.log.append(("send", text, _buttons(reply_markup), parse_mode))
        sent = FakeMessage(self.chat, text)
        sent.markup = reply_markup
        return sent

    async def edit_text(self, text, reply_markup=None, parse_mode=None, **_):
        self.text, self.markup = text, reply_markup
        self.chat.log.append(("edit", text, _buttons(reply_markup), parse_mode))

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup
        self.chat.log.append(("buttons_removed",))

    async def reply_document(self, *args, **kwargs):
        raise AssertionError("no file may ever be sent")


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


class FakeQuery:
    def __init__(self, data, message):
        self.data, self.message, self.answered = data, message, False

    async def answer(self, *args, **kwargs):
        self.answered = True


class FakeBot:
    async def send_chat_action(self, **_):
        pass


def ctx():
    return SimpleNamespace(chat_data={}, bot=FakeBot())


def typed(chat: Chat, text: str, user_id: int = 1):
    return SimpleNamespace(effective_message=FakeMessage(chat, text), effective_user=SimpleNamespace(id=user_id),
                           callback_query=None)


def tapped(chat: Chat, data: str, menu: FakeMessage | None = None):
    menu = menu or FakeMessage(chat, "menu")
    query = FakeQuery(data, menu)
    return SimpleNamespace(effective_message=menu, effective_user=SimpleNamespace(id=1), callback_query=query), query


@pytest.fixture
def doc(monkeypatch, tmp_path):
    fake = {"text": FC27, "reads": 0, "writes": [], "delay": 0.0}

    async def read_doc():
        fake["reads"] += 1
        return gdoc.DocSnapshot(text=gdoc.normalise(fake["text"]), revision_id="rev", end_index=len(fake["text"]) + 2)

    async def replace_doc(new_text, snapshot):
        fake["writes"].append(new_text)
        fake["text"] = new_text

    async def run_humanize(script):
        await asyncio.sleep(fake["delay"])
        text = script.replace("genuinely ", "").replace("Let's talk about", "Next,")
        return StepOutcome(text=text.strip() + "\n", report_lines=["📝 <b>Words:</b> 1 → 1"])

    async def run_emotion(script):
        await asyncio.sleep(fake["delay"])
        return StepOutcome(text="[thoughtful] " + script.strip() + "\n", report_lines=["🎭 <b>Cues added:</b> 1"])

    monkeypatch.setattr(gdoc, "read_doc", read_doc)
    monkeypatch.setattr(gdoc, "replace_doc", replace_doc)
    monkeypatch.setattr(docflow, "run_humanize", run_humanize)
    monkeypatch.setattr(docflow, "run_emotion", run_emotion)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "GOOGLE_DOC_ID", "doc123")
    monkeypatch.setattr(config, "DOC_WRITE_ENABLED", True)
    monkeypatch.setattr(config, "CUE_STYLE", "brackets")
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", frozenset())
    monkeypatch.setattr(config, "LEGACY_TXT_FLOW", False)
    return fake


def run(coro):
    return asyncio.run(coro)


def humanize_by_tap(chat):
    update, query = tapped(chat, docmode.CB_HUMANIZE)
    run(docmode.on_button(update, ctx()))
    assert query.answered
    return update.effective_message


def emotion_by_tap(chat, menu=None):
    update, _ = tapped(chat, docmode.CB_EMOTION, menu)
    run(docmode.on_button(update, ctx()))


def last(chat):
    return chat.log[-1]


# --- 1. routing ------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hi", "update", "go", "humanize this please"])
def test_any_text_shows_menu(doc, text):
    chat = Chat()
    run(on_text_or_menu(typed(chat, text), ctx()))
    kind, msg, buttons, mode = last(chat)
    assert msg == f"📄 Your script is in the Doc: {LINK}\nWhat do you want to do?"
    assert buttons == ["🧹 Go Humanize", "🎭 Add Emotion", "🔄 Cancel / New Script"] and mode == "HTML"
    assert doc["reads"] == 0  # typed text is never treated as a script


def test_typed_go_humanize_runs_humanize(doc):
    chat = Chat()
    run(docmode.on_go_humanize(typed(chat, "go humanize"), ctx()))
    assert chat.log[0][1] == "⏳ Processing..."
    assert len(doc["writes"]) == 1
    assert last(chat)[0] == "edit" and last(chat)[1].startswith("✅ <b>Script humanized</b>")


def test_help_has_new_flow_and_menu(doc):
    chat = Chat()
    run(start(typed(chat, "/help"), ctx()))
    kind, msg, buttons, mode = last(chat)
    assert "🧹 Go Humanize" in msg and "🎭 Add Emotion" in msg and "🔄 Cancel / New Script" in msg and LINK in msg
    assert buttons == ["🧹 Go Humanize", "🎭 Add Emotion", "🔄 Cancel / New Script"] and mode == "HTML"
    assert help_text() == msg


def test_legacy_paths_still_point_to_doc(doc):
    for handler in (intake.on_document, intake.on_go):
        chat = Chat()
        run(handler(typed(chat, "x"), ctx()))
        assert chat.log == [("send", intake.DOC_POINTER, [], None)]


# --- 2-3. humanize then emotion ----------------------------------------------------


def test_humanize_button_edits_menu_and_offers_two_buttons(doc):
    chat = Chat()
    menu = humanize_by_tap(chat)
    assert [e[:2] for e in chat.log] == [("edit", "⏳ Processing..."), ("edit", menu.text)]
    kind, report, buttons, mode = last(chat)
    assert report.startswith(f"✅ <b>Script humanized</b>\n📄 <b>Doc:</b> {LINK}\n\n📝 <b>Words:</b>")
    assert report.endswith(docmode.HUMANIZE_FOOTER)
    assert buttons == ["🎭 Add Emotion", "🔄 Cancel / New Script"] and mode == "HTML"
    saved = state.load()
    assert saved["session_closed"] is False and saved["last_written_text"] == doc["text"]
    assert saved["last_humanize_hash"] == state.text_hash(doc["text"])


def test_emotion_after_humanize(doc):
    chat = Chat()
    menu = humanize_by_tap(chat)
    emotion_by_tap(chat, menu)
    assert doc["text"].startswith("[thoughtful] ") and len(doc["writes"]) == 2
    kind, report, buttons, _ = last(chat)
    assert report.startswith(f"🎭 <b>Emotions added</b>\n📄 <b>Doc:</b> {LINK}")
    assert report.endswith("🔄 Done. Clear the Doc, paste your next script, then send any message.")
    assert buttons == []
    assert state.load()["last_emotion_hash"] == state.text_hash(doc["text"])


# --- 4. Add Emotion refusals ---------------------------------------------------


def test_emotion_before_humanize_is_refused(doc):
    chat = Chat()
    emotion_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_HUMANIZE_FIRST and doc["writes"] == []


def test_emotion_twice_is_refused(doc):
    chat = Chat()
    humanize_by_tap(chat)
    emotion_by_tap(chat)
    emotion_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_EMOTION_DONE and len(doc["writes"]) == 2


# --- 5-6. cancel ------------------------------------------------------------------


@pytest.mark.parametrize("via", ["button", "command"])
def test_cancel_never_touches_doc_and_blocks_emotion(doc, via):
    chat = Chat()
    humanize_by_tap(chat)
    before_text, reads, writes = doc["text"], doc["reads"], len(doc["writes"])
    if via == "button":
        update, _ = tapped(chat, docmode.CB_CANCEL)
        run(docmode.on_button(update, ctx()))
        assert ("buttons_removed",) in chat.log
    else:
        run(docmode.on_cancel_command(typed(chat, "/cancel"), ctx()))
    assert last(chat)[1] == ("🔄 Session cancelled. Clear the old script from the Doc, paste your new script, "
                             f"then send any message.\n📄 {LINK}")
    assert doc["text"] == before_text and doc["reads"] == reads and len(doc["writes"]) == writes
    assert state.load()["last_humanize_hash"]  # hashes kept
    emotion_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_SESSION_CLOSED


def test_cancel_during_job_aborts_before_write(doc):
    doc["delay"] = 0.3
    chat = Chat()

    async def scenario():
        update, _ = tapped(chat, docmode.CB_HUMANIZE)
        job = asyncio.create_task(docmode.on_button(update, ctx()))
        await asyncio.sleep(0.1)
        cancel_update, _ = tapped(chat, docmode.CB_CANCEL)
        await docmode.on_button(cancel_update, ctx())
        await job

    run(scenario())
    texts = [e[1] for e in chat.log if len(e) > 1]
    assert docmode.MSG_CANCELLING in texts
    assert texts[-1] == docmode.MSG_CANCELLED_JOB
    assert doc["writes"] == [] and doc["text"] == FC27


def test_cancel_after_write_says_too_late(doc, monkeypatch):
    chat = Chat()

    async def slow_replace(new_text, snapshot):
        await asyncio.sleep(0.3)
        doc["writes"].append(new_text)
        doc["text"] = new_text

    monkeypatch.setattr(gdoc, "replace_doc", slow_replace)

    async def scenario():
        update, _ = tapped(chat, docmode.CB_HUMANIZE)
        job = asyncio.create_task(docmode.on_button(update, ctx()))
        await asyncio.sleep(0.1)  # job is inside the write now
        await docmode.on_button(tapped(chat, docmode.CB_CANCEL)[0], ctx())
        await job

    run(scenario())
    texts = [e[1] for e in chat.log if len(e) > 1]
    assert docmode.MSG_CANCEL_TOO_LATE in texts and len(doc["writes"]) == 1


# --- 7-8. same script / old script still in the Doc ---------------------------------


def test_same_script_refused_after_humanize_and_after_emotion_across_restart(doc, monkeypatch):
    chat = Chat()
    humanize_by_tap(chat)
    monkeypatch.setattr(docmode, "_job", None)  # "restart": only the files under STATE_DIR survive
    humanize_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_SAME

    emotion_by_tap(chat)
    humanize_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_SAME

    doc["text"] = FC27  # the original raw script pasted again
    humanize_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_SAME
    assert len(doc["writes"]) == 2


def test_new_script_pasted_below_old_is_refused(doc):
    chat = Chat()
    humanize_by_tap(chat)
    doc["text"] = doc["text"] + "\n\n" + FOOTBALL
    humanize_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_OLD_BELOW and len(doc["writes"]) == 1


def test_fresh_script_after_cancel_works(doc):
    chat = Chat()
    humanize_by_tap(chat)
    run(docmode.on_cancel_command(typed(chat, "/cancel"), ctx()))
    doc["text"] = FOOTBALL
    humanize_by_tap(chat)
    assert last(chat)[1].startswith("✅ <b>Script humanized</b>")
    assert state.load()["session_closed"] is False


# --- pre-flight: empty / short ------------------------------------------------------


@pytest.mark.parametrize("text", ["", "  \n\n\t "])
def test_empty_doc(doc, text):
    doc["text"] = text
    chat = Chat()
    humanize_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_EMPTY


def test_short_script(doc):
    doc["text"] = "Just a few words, not a script."
    chat = Chat()
    run(docmode.on_go_humanize(typed(chat, "go humanize"), ctx()))
    assert last(chat)[1].startswith("That's only 7 words, too short to be a script.")


# --- 9. double tap ----------------------------------------------------------------


def test_double_tap_runs_one_job(doc):
    doc["delay"] = 0.2
    chat = Chat()

    async def scenario():
        first = asyncio.create_task(docmode.on_button(tapped(chat, docmode.CB_HUMANIZE)[0], ctx()))
        await asyncio.sleep(0.05)
        await docmode.on_button(tapped(chat, docmode.CB_HUMANIZE)[0], ctx())
        await docmode.on_button(tapped(chat, docmode.CB_EMOTION)[0], ctx())
        await first

    run(scenario())
    assert [e[1] for e in chat.log].count(docmode.MSG_BUSY) == 2
    assert len(doc["writes"]) == 1


# --- 11. a Doc that already has cues -------------------------------------------------


def test_doc_with_cues_humanizes_then_emotion_refuses(doc):
    doc["text"] = FC27.replace("Let's address", "[curious] Let's address", 1)
    chat = Chat()
    humanize_by_tap(chat)
    assert "[curious] Let's address" in doc["text"]
    emotion_by_tap(chat)
    assert last(chat)[1] == docmode.MSG_HAS_CUES and len(doc["writes"]) == 1


# --- 12. failure before the write -----------------------------------------------------


def test_gate_failure_leaves_doc_untouched(doc, monkeypatch):
    async def failing(script):
        raise GateFailure("Go Humanize", ["Short 2 header is missing"])

    monkeypatch.setattr(docflow, "run_humanize", failing)
    chat = Chat()
    humanize_by_tap(chat)
    assert last(chat)[1] == ("Couldn't finish: Go Humanize check failed twice - Short 2 header is missing. "
                             "The Doc was not changed.")
    assert doc["writes"] == [] and doc["text"] == FC27


def test_google_write_failure_leaves_state_untouched(doc, monkeypatch):
    async def broken(new_text, snapshot):
        raise gdoc.DocError("Google Docs error (500): <backend> & co")

    monkeypatch.setattr(gdoc, "replace_doc", broken)
    chat = Chat()
    humanize_by_tap(chat)
    assert last(chat)[1] == ("Couldn't finish: writing the Doc failed - Google Docs error (500): "
                             "&lt;backend&gt; &amp; co. The Doc was not changed.")
    assert "last_humanize_hash" not in state.load()


def test_dry_run_reports_without_writing_or_recording(doc, monkeypatch):
    monkeypatch.setattr(config, "DOC_WRITE_ENABLED", False)
    chat = Chat()
    humanize_by_tap(chat)
    assert last(chat)[1].startswith(docmode.DRY_RUN_TITLE)
    assert doc["writes"] == [] and state.load() == {}


# --- 13. unauthorised users -------------------------------------------------------------


def test_unauthorised_user_is_refused(doc, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", frozenset({999}))
    chat = Chat()
    run(docmode.on_go_humanize(typed(chat, "go humanize"), ctx()))
    assert doc["reads"] == 0 and last(chat)[1].startswith("This bot is private.")


def test_missing_google_key_gets_a_reply_not_silence(doc, monkeypatch):
    async def read_doc():
        raise gdoc.DocError(gdoc.MISSING_CREDENTIALS)

    monkeypatch.setattr(gdoc, "read_doc", read_doc)
    chat = Chat()
    humanize_by_tap(chat)
    assert last(chat)[1] == f"Couldn't read the Doc: {gdoc.MISSING_CREDENTIALS}"
    assert doc["writes"] == []
