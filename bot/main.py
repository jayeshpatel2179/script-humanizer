import logging

from telegram import LinkPreviewOptions, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import config, gdoc
from bot.docmode import GO_HUMANIZE_RE, on_go_humanize
from bot.intake import is_allowed, on_cancel, on_document, on_go, on_text

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=config.LOG_LEVEL)
# httpx logs every request URL at INFO, and Telegram URLs embed the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def help_text() -> str:
    doc = gdoc.doc_url() if config.GOOGLE_DOC_ID else "(Doc not configured)"
    return (
        "I clean up finished video scripts so they sound human.\n\n"
        f"1. Paste the script into the Google Doc: {doc}\n"
        "2. Send me: go humanize\n\n"
        "I replace the script in the Doc with the cleaned version and send you the link with a short report.\n\n"
        "I remove repeated and filler words, vary repeated openers, rewrite \"it's not X, it's Y\" lines and "
        "simplify hard words. Emotion cues, editor notes, short headers and the pronunciation list stay as they are."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    await update.effective_message.reply_text(help_text(), link_preview_options=LinkPreviewOptions(is_disabled=True))


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing an update", exc_info=context.error)


def build_application() -> Application:
    token = config.require("TELEGRAM_BOT_TOKEN")
    config.require("OPENAI_API_KEY")
    if not config.ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty - anyone who finds the bot can use it.")

    # Concurrent updates so one 60-second edit doesn't freeze the bot for other chats.
    application = Application.builder().token(token).concurrent_updates(True).build()
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("go", on_go))
    application.add_handler(CommandHandler("cancel", on_cancel))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, on_document))
    # Must come before on_text, or "go humanize" would be buffered as part of a pasted script.
    application.add_handler(MessageHandler(filters.Regex(GO_HUMANIZE_RE) & ~filters.COMMAND, on_go_humanize))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
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
