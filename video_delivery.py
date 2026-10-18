import asyncio
import logging
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path

import aiosqlite
from aiogram import Bot
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, Message


logger = logging.getLogger(__name__)
PERMANENT_BOT_URL = "https://t.me/gerogroup"
PERMANENT_BOT_CAPTION = (
    f'<a href="{PERMANENT_BOT_URL}">'
    f"Ссылка на постоянного бота тут: {PERMANENT_BOT_URL}</a>"
)
FileIdUpdater = Callable[[str | None], Awaitable[None]]


def build_video_caption(description: str | None = None) -> str:
    clean_description = description.strip() if description else ""
    if not clean_description:
        return PERMANENT_BOT_CAPTION
    max_description_length = 1024 - len(PERMANENT_BOT_CAPTION) - 2
    escaped_description = escape(clean_description)
    if len(escaped_description) > max_description_length:
        escaped_characters: list[str] = []
        current_length = 0
        for character in clean_description:
            escaped_character = escape(character)
            if current_length + len(escaped_character) > max_description_length:
                break
            escaped_characters.append(escaped_character)
            current_length += len(escaped_character)
        escaped_description = "".join(escaped_characters)
    return f"{escaped_description}\n\n{PERMANENT_BOT_CAPTION}"


async def send_video_with_retry(
    bot: Bot,
    chat_id: int | str,
    video: str | FSInputFile,
    description: str | None = None,
) -> Message:
    caption = build_video_caption(description)

    for attempt in (1, 2):
        try:
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            return await bot.send_video(
                chat_id=chat_id,
                video=video,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except TelegramForbiddenError:
            logger.warning("Video delivery blocked by recipient: chat_id=%s", chat_id)
            raise
        except TelegramRetryAfter as error:
            logger.warning(
                "Video delivery FloodWait: attempt=%s chat_id=%s retry_after=%s",
                attempt,
                chat_id,
                error.retry_after,
            )
            if attempt == 2:
                raise
            await asyncio.sleep(error.retry_after)
        except (TelegramNetworkError, TelegramServerError, TimeoutError) as error:
            logger.warning(
                "Video delivery transient error: attempt=%s chat_id=%s error=%s",
                attempt,
                chat_id,
                error,
                exc_info=True,
            )
            if attempt == 2:
                raise
            await asyncio.sleep(1.5)

    raise RuntimeError("Video delivery retry loop ended unexpectedly")


async def send_catalog_video(
    bot: Bot,
    chat_id: int | str,
    media_id: str,
    file_path: str | Path,
    cached_file_id: str | None,
    update_file_id: FileIdUpdater,
    description: str | None = None,
) -> Message:
    path = Path(file_path)
    if cached_file_id:
        try:
            return await send_video_with_retry(
                bot, chat_id, cached_file_id, description
            )
        except TelegramBadRequest:
            logger.warning(
                "Cached Telegram file_id was rejected; re-uploading media_id=%s",
                media_id,
                exc_info=True,
            )
            try:
                await update_file_id(None)
            except aiosqlite.Error:
                logger.exception("Could not clear rejected file_id for media_id=%s", media_id)

    if not path.is_file():
        raise FileNotFoundError(path)

    sent_message = await send_video_with_retry(
        bot, chat_id, FSInputFile(path), description
    )
    if sent_message.video:
        try:
            await update_file_id(sent_message.video.file_id)
        except aiosqlite.Error:
            logger.exception(
                "Video delivered but Telegram file_id could not be cached: media_id=%s",
                media_id,
            )
    return sent_message
