"""Telegram side of Doc mode: a 3-button menu over one Google Doc.

  🧹 Go Humanize   - humanize only, replace the Doc, offer Add Emotion
  🎭 Add Emotion   - add cues to the humanized script in the Doc, replace it
  🔄 Cancel / New  - close the session; never touches the Doc

Nothing is ever sent as a file. Each run ends as one message: "⏳ ..." is
edited into the report or the failure. The Doc is written only after the
output passed its gate; any failure before the write leaves it untouched.
"""

import asyncio
import html
import logging
import re
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import config, docflow, gdoc, state
from bot.docflow import GateFailure, StepOutcome
from bot.intake import TELEGRAM_TEXT_LIMIT, is_allowed
from bot.llm import HumanizeError
from bot.scan import word_count

logger = logging.getLogger(__name__)

# Whole message only: "go humanize", "Go Humanise!", " go  humanize. "
GO_HUMANIZE_RE = re.compile(r"^\s*go\s+humani[sz]e\s*[.!]*\s*$", re.IGNORECASE)

CB_HUMANIZE, CB_EMOTION, CB_CANCEL = "hz:humanize", "hz:emotion", "hz:cancel"
BTN_HUMANIZE = InlineKeyboardButton("🧹 Go Humanize", callback_data=CB_HUMANIZE)
BTN_EMOTION = InlineKeyboardButton("🎭 Add Emotion", callback_data=CB_EMOTION)
BTN_CANCEL = InlineKeyboardButton("🔄 Cancel / New Script", callback_data=CB_CANCEL)
MENU = InlineKeyboardMarkup([[BTN_HUMANIZE, BTN_EMOTION], [BTN_CANCEL]])
AFTER_HUMANIZE = InlineKeyboardMarkup([[BTN_EMOTION, BTN_CANCEL]])

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

MSG_BUSY = "⏳ Already working on a script. I'll send the report when it's done."
MSG_EMPTY = "📭 The Doc is empty. Paste the script into the Doc, then send any message."
MSG_SAME = "♻️ This is the same script I already processed. Clear the Doc, paste a new script, then send any message."
MSG_OLD_BELOW = "⚠️ The Doc still has the old script. Clear it and paste only the new script."
MSG_SESSION_CLOSED = "🔄 That session was cancelled. Paste a new script and send any message."
MSG_EMOTION_DONE = ("🎭 Emotions are already added to this script. Clear the Doc, paste the next script, "
                    "then send any message.")
MSG_HUMANIZE_FIRST = ("🧹 Run Go Humanize on this script first. Add Emotion works on a script I've already "
                      "humanized.")
MSG_HAS_CUES = "🎭 This script already has emotion cues."
MSG_CUES_OFF = "🎭 Emotion cues are switched off (CUE_STYLE=none)."
MSG_CANCELLED_JOB = "🔄 Cancelled. Nothing was changed in the Doc."
MSG_CANCELLING = "🔄 Cancelling - I'll stop before anything is written to the Doc."
MSG_CANCEL_TOO_LATE = "✅ That job had already written the Doc, so Cancel changed nothing."
DRY_RUN_TITLE = "🧪 <b>Dry run - the Doc was NOT changed</b> (result saved on the server)"
HUMANIZE_FOOTER = "👇 Want emotion cues for ElevenLabs? Tap Add Emotion. Skip it if this video doesn't need them."
EMOTION_FOOTER = "🔄 Done. Clear the Doc, paste your next script, then send any message."


@dataclass
class _Job:
    cancel_requested: bool = False
    writing: bool = False  # set right before the Doc write; Cancel is too late from here


_job_lock = asyncio.Lock()
_job: _Job | None = None


class _Stop(Exception):
    """End the run with this message. The Doc has not been written."""


def doc_link() -> str:
    return html.escape(gdoc.doc_url(), quote=False)


def menu_text() -> str:
    return f"📄 Your script is in the Doc: {doc_link()}\nWhat do you want to do?"


async def _send(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    return await message.reply_text(text[:TELEGRAM_TEXT_LIMIT], parse_mode=ParseMode.HTML,
                                    link_preview_options=NO_PREVIEW, reply_markup=markup)


async def _edit(status: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    text = text[:TELEGRAM_TEXT_LIMIT]
    try:
        await status.edit_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
    except TelegramError:
        # e.g. the message was deleted - fall back to a new one.
        logger.warning("Couldn't edit the status message; sending a new one", exc_info=True)
        await _send(status, text, markup)


# --- entry points ------------------------------------------------------------


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any message that isn't a command: show the 3-button menu. Text is never treated as a script."""
    if not await is_allowed(update):
        return
    await _send(update.effective_message, menu_text(), MENU)


async def on_go_humanize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    await _guarded(context, update.effective_message, None, _humanize)


async def on_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    await _cancel(update.effective_message, None)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # stop the button's loading spinner straight away
    if not await is_allowed(update):
        return
    pressed = query.message
    if query.data == CB_HUMANIZE:
        await _guarded(context, pressed, pressed, _humanize)
    elif query.data == CB_EMOTION:
        await _guarded(context, pressed, pressed, _emotion)
    elif query.data == CB_CANCEL:
        await _cancel(pressed, pressed)


async def _guarded(context, message: Message, pressed: Message | None, step) -> None:
    global _job
    if not config.GOOGLE_DOC_ID:
        await _send(message, "Doc mode isn't set up yet (GOOGLE_DOC_ID is missing).")
        return
    if _job_lock.locked():
        await _send(message, MSG_BUSY)
        return
    async with _job_lock:
        _job = _Job()
        typing = asyncio.create_task(_keep_typing(context, message.chat_id))
        try:
            await step(message, pressed)
        finally:
            typing.cancel()
            _job = None


# --- 🧹 Go Humanize --------------------------------------------------------------


async def _read(message: Message) -> gdoc.DocSnapshot | None:
    try:
        snapshot = await gdoc.read_doc()
    except gdoc.DocError as exc:
        await _send(message, f"Couldn't read the Doc: {html.escape(str(exc), quote=False)}")
        return None
    if not snapshot.text.strip():
        await _send(message, MSG_EMPTY)
        return None
    return snapshot


def _contains_old_plus_new(doc_text: str, saved: dict) -> bool:
    old = docflow.normalise_ws(saved.get("last_written_text") or "")
    now = docflow.normalise_ws(doc_text)
    return bool(old) and old in now and now != old


async def _humanize(message: Message, pressed: Message | None) -> None:
    # Pre-flight: read-only, no model calls.
    snapshot = await _read(message)
    if snapshot is None:
        return
    script, saved = snapshot.text, state.load()
    script_hash = state.text_hash(script)
    if script_hash in {saved.get(k) for k in ("last_input_hash", "last_humanize_hash", "last_emotion_hash")} - {None}:
        await _send(message, MSG_SAME)
        return
    if _contains_old_plus_new(script, saved):
        await _send(message, MSG_OLD_BELOW)
        return
    words = word_count(script)
    if words < config.DOC_MIN_WORDS:
        await _send(message, f"That's only {words} words, too short to be a script. "
                             "Paste the full script into the Doc, then send: go humanize")
        return

    status = await _status(message, pressed, "⏳ Processing...")
    try:
        _backup(script)
        outcome = await _run_step(docflow.run_humanize, script)
        written = await _write(outcome, snapshot)
    except _Stop as stop:
        await _edit(status, str(stop))
        return
    if written is None:  # dry run
        await _edit(status, _report(DRY_RUN_TITLE, outcome, outcome.text, HUMANIZE_FOOTER))
        return
    state.update(last_input_hash=script_hash, last_humanize_hash=state.text_hash(written),
                 last_output_hash=state.text_hash(written), last_written_text=outcome.text, session_closed=False)
    report = _report("✅ <b>Script humanized</b>", outcome, written, HUMANIZE_FOOTER)
    await _edit(status, report, AFTER_HUMANIZE)


# --- 🎭 Add Emotion ----------------------------------------------------------------


async def _emotion(message: Message, pressed: Message | None) -> None:
    if config.CUE_STYLE not in ("brackets", "parentheses"):
        await _send(message, MSG_CUES_OFF)
        return
    snapshot = await _read(message)
    if snapshot is None:
        return
    script, saved = snapshot.text, state.load()
    script_hash = state.text_hash(script)
    if saved.get("session_closed"):
        await _send(message, MSG_SESSION_CLOSED)
        return
    if script_hash == saved.get("last_emotion_hash"):
        await _send(message, MSG_EMOTION_DONE)
        return
    if script_hash != saved.get("last_humanize_hash"):
        await _send(message, MSG_HUMANIZE_FIRST)
        return
    if docflow.cue_sequence(script):
        await _send(message, MSG_HAS_CUES)
        return

    status = await _status(message, pressed, "⏳ Adding emotions...")
    try:
        _backup(script)
        outcome = await _run_step(docflow.run_emotion, script)
        written = await _write(outcome, snapshot)
    except _Stop as stop:
        await _edit(status, str(stop))
        return
    if written is None:  # dry run
        await _edit(status, _report(DRY_RUN_TITLE, outcome, outcome.text, EMOTION_FOOTER))
        return
    state.update(last_emotion_hash=state.text_hash(written), last_output_hash=state.text_hash(written),
                 last_written_text=outcome.text)
    await _edit(status, _report("🎭 <b>Emotions added</b>", outcome, written, EMOTION_FOOTER))


# --- shared processing ---------------------------------------------------------


async def _status(message: Message, pressed: Message | None, text: str) -> Message:
    """Turn the pressed menu into the status message (buttons removed), or send a new one."""
    if pressed is not None:
        await _edit(pressed, text)
        return pressed
    return await _send(message, text)


async def _run_step(step, script: str) -> StepOutcome:
    try:
        return await step(script)
    except HumanizeError as exc:
        raise _Stop(f"Couldn't finish: {html.escape(str(exc).rstrip('.'), quote=False)}. "
                    "The Doc was not changed.") from exc
    except GateFailure as exc:
        detail = html.escape(f"{exc.stage} check failed twice - {'; '.join(exc.failures)}", quote=False)
        raise _Stop(f"Couldn't finish: {detail}. The Doc was not changed.") from exc
    except Exception as exc:
        logger.exception("Step failed")
        raise _Stop("Couldn't finish: unexpected error (details in the bot log). The Doc was not changed.") from exc


def _backup(script: str) -> None:
    try:
        state.save_backup(script)
    except Exception as exc:
        logger.exception("Backup failed")
        raise _Stop("Couldn't finish: saving a backup of the script failed. The Doc was not changed.") from exc


async def _write(outcome: StepOutcome, snapshot: gdoc.DocSnapshot) -> str | None:
    """Write the Doc and return the text that's in it now. None in dry run (nothing written or recorded)."""
    job = _job
    if job is not None and job.cancel_requested:
        raise _Stop(MSG_CANCELLED_JOB)
    if job is not None:
        job.writing = True  # no await between the check and this flag, so Cancel can't slip in
    if not config.DOC_WRITE_ENABLED:
        state.save_backup(outcome.text, prefix="dry_run")
        return None
    try:
        await gdoc.replace_doc(outcome.text, snapshot)
    except gdoc.DocError as exc:
        raise _Stop(f"Couldn't finish: writing the Doc failed - {html.escape(str(exc), quote=False)}. "
                    "The Doc was not changed.") from exc
    try:
        return (await gdoc.read_doc()).text
    except Exception:
        logger.exception("Re-read after write failed")
        outcome.report_lines.append("⚠️ <b>Double-check:</b> the Doc was updated, but I couldn't re-read it.")
        return outcome.text


def _report(title: str, outcome: StepOutcome, written: str, footer: str) -> str:
    lines = [title, f"📄 <b>Doc:</b> {doc_link()}", "", *outcome.report_lines]
    if state.text_hash(written) != state.text_hash(outcome.text):
        lines.append("⚠️ <b>Double-check:</b> the Doc's text after writing doesn't exactly match what was sent.")
    return "\n".join([*lines, "", footer])


# --- 🔄 Cancel / New Script ---------------------------------------------------------


async def _cancel(message: Message, pressed: Message | None) -> None:
    """Never reads, writes or clears the Doc. Stored hashes are kept."""
    job = _job
    if job is not None:
        if job.writing:
            await _send(message, MSG_CANCEL_TOO_LATE)
        else:
            job.cancel_requested = True
            state.update(session_closed=True)
            await _send(message, MSG_CANCELLING)
        return
    state.update(session_closed=True)
    if pressed is not None:
        try:
            await pressed.edit_reply_markup(reply_markup=None)
        except TelegramError:
            logger.debug("Couldn't remove buttons from the pressed message", exc_info=True)
    await _send(message, "🔄 Session cancelled. Clear the old script from the Doc, paste your new script, "
                         f"then send any message.\n📄 {doc_link()}")


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("typing indicator failed", exc_info=True)
