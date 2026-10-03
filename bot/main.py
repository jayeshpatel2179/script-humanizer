import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot import config
from bot.docmode import (
    GO_HUMANIZE_RE,
    MENU,
    NO_PREVIEW,
    doc_link,
    on_button,
    on_cancel_command,
    on_go_humanize,
    on_menu,
)
from bot.intake import is_allowed, on_document, on_go, on_text

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=config.LOG_LEVEL)
# httpx logs every request URL at INFO, and Telegram URLs embed the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def help_text() -> str:
    doc = doc_link() if config.GOOGLE_DOC_ID else "(Doc not configured)"
    return (
        "I clean up finished video scripts so they sound human.\n\n"
        f"1. Paste the script into the Google Doc: {doc}\n"
        "2. Send me any message and tap <b>🧹 Go Humanize</b>. I clean the wording and replace the script in "
        "the Doc, then send you the link with a short report.\n"
        "3. Optional: tap <b>🎭 Add Emotion</b> to add ElevenLabs emotion cues to the humanized script.\n\n"
        "Tap <b>🔄 Cancel / New Script</b> to start over with a different script. It never changes the Doc.\n\n"
        "I remove repeated and filler words, vary repeated openers, rewrite \"it's not X, it's Y\" lines and "
        "simplify hard words. Editor notes, short headers and the pronunciation list stay as they are."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML,
                                              link_preview_options=NO_PREVIEW, reply_markup=MENU)


async def on_text_or_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Typed text shows the menu. Only with LEGACY_TXT_FLOW on is it buffered as a pasted script."""
    if config.LEGACY_TXT_FLOW:
        await on_text(update, context)
    else:
        await on_menu(update, context)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing an update", exc_info=context.error)


def build_application() -> Application:
    token = config.require("TELEGRAM_BOT_TOKEN")
    config.require("OPENAI_API_KEY")
    if not config.ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty - anyone who finds the bot can use it.")

    # Concurrent updates so a Cancel tap is handled while a 60-second job runs.
    application = Application.builder().token(token).concurrent_updates(True).build()
    # Order matters: the first matching handler wins.
    # 1. Buttons
    application.add_handler(CallbackQueryHandler(on_button, pattern=r"^hz:"))
    # 2. Exact commands
    application.add_handler(MessageHandler(filters.Regex(GO_HUMANIZE_RE) & ~filters.COMMAND, on_go_humanize))
    application.add_handler(CommandHandler("cancel", on_cancel_command))
    application.add_handler(CommandHandler(["start", "help"], start))
    # 3. Old .txt / paste / /go flow (each replies with a pointer to the Doc while LEGACY_TXT_FLOW is off)
    application.add_handler(CommandHandler("go", on_go))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, on_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED, on_text_or_menu))
    # 4. Anything else (other commands, stickers, photos...): the menu
    application.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED, on_menu))
    application.add_error_handler(_error_handler)
    return application


def main() -> None:
    application = build_application()
    logger.info("Starting Script Humanizer bot (polling, model=%s, doc mode=%s, doc writes=%s, cues=%s)...",
                config.OPENAI_MODEL, f"tab {config.GOOGLE_DOC_TAB_ID}" if config.GOOGLE_DOC_ID else "off",
                "ON" if config.DOC_WRITE_ENABLED else "dry run", config.CUE_STYLE)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
