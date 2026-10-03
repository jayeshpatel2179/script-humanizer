"""Telegram side of Doc mode: "go humanize" reads the Google Doc, processes it
and replaces it.

Nothing is ever sent to Telegram as a file. Each run ends as one message:
"Processing..." is edited into the final report (or the failure). Nothing in
the Doc changes until the final text has passed the validation gate.
"""

import asyncio
import logging
import re

from telegram import LinkPreviewOptions, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import config, gdoc, state
from bot.docflow import GateFailure, process_script
from bot.intake import TELEGRAM_TEXT_LIMIT, is_allowed
from bot.llm import HumanizeError
from bot.scan import word_count

logger = logging.getLogger(__name__)

# Whole message only: "go humanize", "Go Humanise!", " go  humanize. "
GO_HUMANIZE_RE = re.compile(r"^\s*go\s+humani[sz]e\s*[.!]*\s*$", re.IGNORECASE)

ASK_TO_PASTE = "Paste the script into the Doc, then send: go humanize"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_job_lock = asyncio.Lock()


async def on_go_humanize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    message = update.effective_message
    if not config.GOOGLE_DOC_ID:
        await message.reply_text("Doc mode isn't set up yet (GOOGLE_DOC_ID is missing).")
        return
    # 1. Job lock
    if _job_lock.locked():
        await message.reply_text("Already working on a script. I'll send the report when it's done.")
        return
    async with _job_lock:
        typing = asyncio.create_task(_keep_typing(context, message.chat_id))
        try:
            await _run(message)
        finally:
            typing.cancel()


async def _run(message: Message) -> None:
    # --- pre-flight: read-only, no model calls ---
    try:
        snapshot = await gdoc.read_doc()  # 2. read the tab
    except gdoc.DocError as exc:
        await message.reply_text(f"Couldn't read the Doc: {exc}")
        return
    script = snapshot.text
    if not script.strip():  # 3. empty
        await message.reply_text(f"The Doc is empty. {ASK_TO_PASTE}")
        return
    saved = state.load()
    script_hash = state.text_hash(script)
    if script_hash in (saved.get("last_output_hash"), saved.get("last_input_hash")):  # 4. same script
        await message.reply_text("This is the same script I already processed. Paste a new script into the Doc, "
                                 "then send: go humanize")
        return
    words = word_count(script)
    if words < config.DOC_MIN_WORDS:  # 5. too short
        await message.reply_text(f"That's only {words} words, too short to be a script. "
                                 f"Paste the full script into the Doc, then send: go humanize")
        return

    # --- processing: one message, edited at the end ---
    status = await message.reply_text(f"Processing {words:,} words... this takes about a minute.")
    try:
        backup = state.save_backup(script)
        logger.info("Backed up the Doc's raw script to %s", backup)
        report = await _process_and_write(script, script_hash, snapshot)
    except _Failed as exc:
        report = f"Couldn't finish: {exc}. The Doc was not changed."
    except Exception:
        logger.exception("Doc mode failed")
        report = "Couldn't finish: unexpected error (details in the bot log). The Doc was not changed."
    await _edit(status, report)


class _Failed(Exception):
    """A failure before the Doc write. The message names the check that failed."""


async def _process_and_write(script: str, script_hash: str, snapshot: gdoc.DocSnapshot) -> str:
    try:
        outcome = await process_script(script)
    except HumanizeError as exc:
        raise _Failed(str(exc).rstrip(".")) from exc
    except GateFailure as exc:
        raise _Failed(f"{exc.stage} check failed twice - {'; '.join(exc.failures)}") from exc

    warnings = list(outcome.warnings)
    if not config.DOC_WRITE_ENABLED:
        saved_to = state.save_backup(outcome.text, prefix="dry_run")
        header = f"Dry run - the Doc was NOT changed. Result saved on the server: {saved_to.name}"
    else:
        try:
            await gdoc.replace_doc(outcome.text, snapshot)
        except gdoc.DocError as exc:
            raise _Failed(f"writing the Doc failed - {exc}") from exc
        # From here the Doc has changed, so failures below are reported, not raised.
        header = "Script humanized"
        try:
            written = await gdoc.read_doc()
            state.update(last_output_hash=state.text_hash(written.text), last_input_hash=script_hash)
            if state.text_hash(written.text) != state.text_hash(outcome.text):
                warnings.insert(0, "The Doc's text after writing doesn't exactly match what was sent - check it.")
        except Exception:
            logger.exception("Post-write verification failed")
            warnings.insert(0, "The Doc was updated, but I couldn't re-read it or save its hash - check it.")

    lines = [header, f"Doc: {gdoc.doc_url()}", "", *outcome.report_lines]
    if warnings:
        lines.append("Double-check: " + " | ".join(warnings))
    return "\n".join(lines)


async def _edit(status: Message, text: str) -> None:
    text = text[:TELEGRAM_TEXT_LIMIT]
    try:
        await status.edit_text(text, link_preview_options=NO_PREVIEW)
    except TelegramError:
        # e.g. the "Processing..." message was deleted - fall back to a new message.
        logger.warning("Couldn't edit the status message; sending a new one", exc_info=True)
        await status.reply_text(text, link_preview_options=NO_PREVIEW)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("typing indicator failed", exc_info=True)
