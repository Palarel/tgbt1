import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import load_dotenv

from aiogram.types import CallbackQuery, Message

from admin import router as admin_router
from catalog import MEDIA_DIR, PROJECT_DIR, scan_media
from database import Database
from handlers import router


class UserTrackingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if user is None:
            return await handler(event, data)

        await data["db"].upsert_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        return await handler(event, data)


class _TransientTelegramNetworkErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "aiogram.dispatcher"
            and record.levelno >= logging.ERROR
            and record.msg == "Failed to fetch updates - %s: %s"
            and isinstance(record.args, tuple)
            and record.args
            and record.args[0] == "TelegramNetworkError"
        ):
            return False
        return True


async def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.dispatcher").addFilter(
        _TransientTelegramNetworkErrorFilter()
    )
    logger = logging.getLogger(__name__)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Set it in the project .env file.")

    database_path = Path(os.getenv("DATABASE_PATH", str(PROJECT_DIR / "shop.sqlite3")))
    if not database_path.is_absolute():
        database_path = PROJECT_DIR / database_path

    db = Database(database_path)
    try:
        await db.initialize()
        media_items = scan_media()
        await db.refresh_catalog(media_items)
        logger.info("Indexed %d media file(s) from %s", len(media_items), MEDIA_DIR)
        if not media_items:
            logger.warning("No supported files found; add photos/videos under ggfiles.")

        bot = Bot(token=token, session=AiohttpSession(limit=100, timeout=60))
        dispatcher = Dispatcher()
        dispatcher["db"] = db
        dispatcher.message.outer_middleware(UserTrackingMiddleware())
        dispatcher.callback_query.outer_middleware(UserTrackingMiddleware())
        dispatcher.include_router(router)
        dispatcher.include_router(admin_router)
        try:
            await dispatcher.start_polling(bot, polling_timeout=30)
        finally:
            await bot.session.close()
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Bot stopped")
