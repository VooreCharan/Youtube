"""
Telegram bot entry point.

    python -m app.bot

Uses long polling, so no public URL / webhook is needed.
"""

from __future__ import annotations

from loguru import logger
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot import handlers
from app.bot.pipeline import ffmpeg_available
from app.config import get_settings
from app.database import init_db
from app.services.llm_service import build_chain
from app.utils.deploy_secrets import materialize_secret_files
from app.utils.logger import setup_logging


async def _post_init(app: Application) -> None:
    settings = get_settings()
    await init_db()

    if not settings.allowed_user_ids:
        logger.warning(
            "TELEGRAM_ALLOWED_USERS is empty — the bot will refuse everyone and reply with their user ID. "
            "Add your ID to .env to unlock it."
        )
    if not ffmpeg_available():
        logger.warning("FFmpeg not found — video rendering will fail until it is installed.")
    if not settings.token_path.exists():
        logger.warning("YouTube is not connected yet — run `python -m app.youtube_auth` once before publishing.")
    chain = build_chain()
    if chain:
        logger.info("AI chain ({} models): {}", len(chain), " → ".join(t.key for t in chain))
    else:
        logger.warning("No AI provider keys set — topic and script generation will fail.")

    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("cancel", "Stop the current video"),
        BotCommand("help", "How it works"),
    ])
    me = await app.bot.get_me()
    logger.info("🤖 Telegram bot @{} is running", me.username)


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env — create a bot with @BotFather first.")

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)  # one user's long render must not block other chats
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(120)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("menu", handlers.cmd_start))
    app.add_handler(CommandHandler("cancel", handlers.cmd_cancel))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CallbackQueryHandler(handlers.on_button))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, handlers.on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))
    app.add_error_handler(handlers.on_error)
    return app


def main() -> None:
    materialize_secret_files()  # YouTube files from env vars (Railway)
    setup_logging()
    get_settings().ensure_directories()
    logger.info("Starting Telegram bot …")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
