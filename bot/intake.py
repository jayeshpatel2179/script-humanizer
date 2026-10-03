"""Telegram handlers: .txt uploads, buffered pastes, /go and /cancel."""

import asyncio
import io
import logging
import time
from datetime import datetime
from pathlib import Path

from telegram import InputFile, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bot import config
from bot.llm import HumanizeError
from bot.pipeline import humanize
from bot.scan import word_count

logger = logging.getLogger(__name__)

BUFFER_KEY = "paste_buffer"
BUSY_KEY = "busy"
POINTER_SENT_KEY = "doc_pointer_sent_at"
TELEGRAM_TEXT_LIMIT = 4096

DOC_POINTER = "I work from the Google Doc now. Paste the script into the Doc, then send: go humanize"
# A long paste arrives as several messages - answer the first, not every chunk.
POINTER_COOLDOWN_SECONDS = 60


async def is_allowed(update: Update) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True  # no allowlist configured: open to anyone who finds the bot
    user = update.effective_user
    if user and user.id in config.ALLOWED_USER_IDS:
        return True
    if update.effective_message:
        user_id = user.id if user else "unknown"
        await update.effective_message.reply_text(
            f"This bot is private. Your Telegram user ID is {user_id} - ask the owner to add it to ALLOWED_USER_IDS."
        )
    return False


async def legacy_flow_off(update: Update, context: ContextTypes.DEFAULT_TYPE, *, cooldown: bool = False) -> bool:
    """With LEGACY_TXT_FLOW off, point the user at the Doc and return True (the handler stops)."""
    if config.LEGACY_TXT_FLOW:
        return False
    now = time.monotonic()
    last = context.chat_data.get(POINTER_SENT_KEY)
    if not (cooldown and last is not None and now - last < POINTER_COOLDOWN_SECONDS):
        await update.effective_message.reply_text(DOC_POINTER)
    context.chat_data[POINTER_SENT_KEY] = now
    return True


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update) or await legacy_flow_off(update, context, cooldown=True):
        return
    buffer: list[str] = context.chat_data.setdefault(BUFFER_KEY, [])
    buffer.append(update.effective_message.text)
    if len(buffer) == 1:
        await update.effective_message.reply_text(
            "📥 Got it. Telegram splits long pastes into several messages, so keep sending if there's more.\n"
            "Send /go when the whole script is in, or /cancel to start over."
        )


async def on_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update) or await legacy_flow_off(update, context):
        return
    buffer: list[str] = context.chat_data.get(BUFFER_KEY) or []
    if not buffer:
        await update.effective_message.reply_text("Nothing to humanize yet. Paste your script first, or send a .txt file.")
        return
    # Keep the buffer until it succeeds, so /go can simply be retried after an error.
    if await _process(update, context, join_chunks(buffer), source_name=None):
        context.chat_data[BUFFER_KEY] = []


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update) or await legacy_flow_off(update, context):
        return
    had = len(context.chat_data.get(BUFFER_KEY) or [])
    context.chat_data[BUFFER_KEY] = []
    await update.effective_message.reply_text(f"Cleared {had} buffered message(s)." if had else "Nothing was buffered.")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update) or await legacy_flow_off(update, context):
        return
    message = update.effective_message
    document = message.document
    name = document.file_name or "script.txt"
    if not (name.lower().endswith(".txt") or document.mime_type == "text/plain"):
        await message.reply_text("Please send the script as a .txt file (or paste it as text).")
        return
    if document.file_size and document.file_size > config.MAX_UPLOAD_BYTES:
        await message.reply_text("That file is too big for a script (limit 1 MB).")
        return
    telegram_file = await document.get_file()
    data = bytes(await telegram_file.download_as_bytearray())
    await _process(update, context, decode(data), source_name=Path(name).stem)


async def _process(update: Update, context: ContextTypes.DEFAULT_TYPE, script: str, source_name: str | None) -> bool:
    message = update.effective_message
    script = script.replace("\r\n", "\n").strip()
    words = word_count(script)
    if words < config.MIN_INPUT_WORDS:
        await message.reply_text(f"That's only {words} words - send the full script.")
        return False
    if len(script) > config.MAX_INPUT_CHARS:
        await message.reply_text("That script is too long for one pass. Split it and send the parts separately.")
        return False
    if context.chat_data.get(BUSY_KEY):
        await message.reply_text("Still working on the previous script - wait for it to finish.")
        return False

    context.chat_data[BUSY_KEY] = True
    typing = asyncio.create_task(_keep_typing(context, message.chat_id))
    await message.reply_text(f"✍️ Humanizing {words:,} words… this usually takes 30–90 seconds.")
    try:
        result = await humanize(script)
    except HumanizeError as exc:
        await message.reply_text(f"❌ {exc}")
        return False
    except Exception:
        logger.exception("Humanize failed")
        await message.reply_text("❌ Something went wrong while humanizing. Try again; if it keeps failing, check the logs.")
        return False
    finally:
        typing.cancel()
        context.chat_data[BUSY_KEY] = False

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{source_name}_humanized.txt" if source_name else f"humanized_{stamp}.txt"
    await message.reply_document(document=InputFile(io.BytesIO(result.text.encode("utf-8")), filename=filename))
    await message.reply_text(result.report[:TELEGRAM_TEXT_LIMIT])
    return True


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("typing indicator failed", exc_info=True)


def join_chunks(chunks: list[str]) -> str:
    """Rejoin a paste Telegram split into several messages.

    A chunk ending mid-sentence was split mid-paragraph, so it's glued back
    with a space; otherwise the split fell at a paragraph or sentence end.
    """
    joined = chunks[0]
    for chunk in chunks[1:]:
        separator = "\n" if joined.rstrip().endswith((".", "!", "?", ")", "]", ":", '"', "”")) else " "
        joined = f"{joined.rstrip()}{separator}{chunk.lstrip()}"
    return joined


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")
