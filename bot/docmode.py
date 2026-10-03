"""Telegram side of Doc mode: "go humanize" reads the Google Doc, processes it
and replaces it.

Nothing in the Doc changes until the final text has passed the validation
gate. Any failure before the write leaves the Doc untouched.
"""

import asyncio
import hashlib
import io
import logging
import re
from datetime import datetime

from telegram import InputFile, LinkPreviewOptions, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bot import config, gdoc
from bot.docflow import GateFailure, process_script
from bot.emotion import cue_pattern
from bot.intake import TELEGRAM_TEXT_LIMIT, is_allowed
from bot.llm import HumanizeError
from bot.scan import word_count

logger = logging.getLogger(__name__)

# Whole message only: "go humanize", "Go Humanise!", " go  humanize. "
GO_HUMANIZE_RE = re.compile(r"^\s*go\s+humani[sz]e\s*[.!]*\s*$", re.IGNORECASE)

# Three or more cues means the Doc already holds processed output.
ALREADY_PROCESSED_CUES = 3

_job_lock = asyncio.Lock()
# Hash of the last text written to the Doc. In memory only: after a restart the
# cue check above still catches a re-run on processed output.
_last_written_hash: str | None = None


def _hash(text: str) -> str:
    return hashlib.sha256(gdoc.normalise(text).encode("utf-8")).hexdigest()


async def on_go_humanize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    message = update.effective_message
    if not config.GOOGLE_DOC_ID:
        await message.reply_text("Doc mode isn't set up yet (GOOGLE_DOC_ID is missing).")
        return
    if _job_lock.locked():
        await message.reply_text("⏳ Already processing a script - wait for it to finish.")
        return
    async with _job_lock:
        typing = asyncio.create_task(_keep_typing(context, message.chat_id))
        try:
            await _run(update)
        except Exception:
            logger.exception("Doc mode failed")
            await message.reply_text("❌ Something went wrong. The Doc was not changed. Check the logs and try again.")
        finally:
            typing.cancel()


async def _run(update: Update) -> None:
    global _last_written_hash
    message = update.effective_message

    # --- pre-flight (read-only) ---
    try:
        snapshot = await gdoc.read_doc()
    except gdoc.DocError as exc:
        await message.reply_text(f"❌ Couldn't read the Doc: {exc}")
        return
    script = snapshot.text
    words = word_count(script)
    if words == 0:
        await message.reply_text("The Doc is empty - paste a script into it first, then send go humanize.")
        return
    if words < config.DOC_MIN_WORDS:
        await message.reply_text(f"The Doc only has {words} words (minimum {config.DOC_MIN_WORDS}). "
                                 "Paste the full script, then send go humanize.")
        return
    if _hash(script) == _last_written_hash:
        await message.reply_text("The Doc still holds the script I already processed. "
                                 "Select all in the Doc, paste the new script, then send go humanize.")
        return
    pattern = cue_pattern()
    if pattern and len(pattern.findall(script)) >= ALREADY_PROCESSED_CUES:
        await message.reply_text("The Doc already contains emotion cues, so it looks already processed - or a new "
                                 "script was pasted under an old result. Select all in the Doc, paste only the new "
                                 "script, then send go humanize.")
        return

    await message.reply_text(f"📄 Read {words:,} words from the Doc. Humanizing and adding emotion - "
                             "this takes 1–3 minutes.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    await message.reply_document(
        document=InputFile(io.BytesIO((script + "\n").encode("utf-8")), filename=f"backup_{stamp}.txt"),
        caption="Backup of the Doc before any change.",
    )

    # --- processing ---
    try:
        outcome = await process_script(script)
    except HumanizeError as exc:
        await message.reply_text(f"❌ {exc}\nThe Doc was not changed.")
        return
    except GateFailure as exc:
        failures = "\n".join(f"   • {f}" for f in exc.failures)
        await message.reply_text(f"❌ {exc.stage} failed its checks twice, so the Doc was not changed:\n{failures}")
        return

    report = "\n".join(outcome.report_lines)
    if outcome.warnings:
        report += "\n\nCheck before recording:\n" + "\n".join(f"   • {w}" for w in outcome.warnings)

    # --- write ---
    if not config.DOC_WRITE_ENABLED:
        await message.reply_document(
            document=InputFile(io.BytesIO(outcome.text.encode("utf-8")), filename=f"dry_run_{stamp}.txt"),
        )
        await message.reply_text(f"🧪 Dry run - the Doc was NOT changed.\n\n{report}"[:TELEGRAM_TEXT_LIMIT])
        return

    try:
        await gdoc.replace_doc(outcome.text, snapshot)
    except gdoc.DocError as exc:
        await message.reply_text(f"❌ Couldn't write the Doc: {exc}\nThe Doc was not changed.")
        return

    written = await gdoc.read_doc()
    _last_written_hash = _hash(written.text)
    if _hash(outcome.text) != _last_written_hash:
        report = ("⚠️ The Doc was written, but reading it back doesn't match the expected text exactly. "
                  "Check it - the backup above has the original.\n\n" + report)
    await message.reply_text(f"✅ Doc updated: {gdoc.doc_url()}\n\n{report}"[:TELEGRAM_TEXT_LIMIT],
                             link_preview_options=LinkPreviewOptions(is_disabled=True))


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("typing indicator failed", exc_info=True)
