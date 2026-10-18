import asyncio
import asyncio
import logging
import os
import random
from pathlib import Path

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from catalog import MEDIA_DIR, scan_media
from database import Database
from video_delivery import send_catalog_video, send_video_with_retry


logger = logging.getLogger(__name__)
router = Router(name="admin")
MEDIA_LABELS = {"photo": "Фото", "video": "Видео"}
PACKAGE_LABELS = {
    "archive": "Базовый архив",
    "full": "FULL PACK — 550 видео",
}
CATALOG_PAGE_SIZE = 8
MANUAL_DELIVERY_FAILURE_MESSAGE = (
    "Не удалось доставить медиаконтент после повторной попытки. "
    "Обратитесь в поддержку для ручного решения: @huskyies"
)
RECIPIENT_DELIVERY_FAILURE_MESSAGE = (
    "Возникла техническая ошибка при получении вашего заказа. "
    "Пожалуйста, напишите в поддержку: @huskyies"
)


class ManualGrantFlow(StatesGroup):
    recipient = State()
    content = State()
    upload = State()
    confirmation = State()
    sending = State()


def is_admin(user: object | None) -> bool:
    if user is None:
        return False
    username = (getattr(user, "username", None) or "").lstrip("@").lower()
    return username == "huskyies"


class AdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if is_admin(event.from_user):
            return True
        if isinstance(event, Message) and event.text:
            command = event.text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
            return command == "/admin"
        return False


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Статус системы", callback_data="admin:health")],
            [InlineKeyboardButton(text="Тестовая выдача", callback_data="admin:tests")],
            [InlineKeyboardButton(text="Прогреть кэш (Предзагрузка)", callback_data="admin:warm")],
            [InlineKeyboardButton(text="Статистика покупок", callback_data="admin:stats")],
            [InlineKeyboardButton(text="Пересканировать ggfiles", callback_data="admin:rescan")],
            [InlineKeyboardButton(text="Выдать контент", callback_data="admin:manual")],
        ]
    )


def tests_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Тест: отправить случайное фото",
                    callback_data="admin:send:photo",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Тест: отправить случайное видео",
                    callback_data="admin:send:video",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="admin:home")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="admin:home")]
        ]
    )


def manual_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="admin:manual:cancel")]
        ]
    )


def manual_content_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выбрать из каталога", callback_data="admin:manual:catalog"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отправить фото/видео", callback_data="admin:manual:upload"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="admin:manual:cancel")],
        ]
    )


def manual_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить", callback_data="admin:manual:confirm"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="admin:manual:cancel"),
            ]
        ]
    )


def recipient_summary(data: dict[str, object]) -> str:
    name = " ".join(
        part
        for part in (
            str(data.get("recipient_first_name", "")),
            str(data.get("recipient_last_name", "")),
        )
        if part
    ) or "имя не сохранено"
    username = data.get("recipient_username")
    username_text = f"@{username}" if username else "не указан"
    return (
        f"Получатель: {name}\n"
        f"Username: {username_text}\n"
        f"user_id: {data['recipient_id']}"
    )


async def show_manual_confirmation(
    message: Message, state: FSMContext
) -> None:
    data = await state.get_data()
    await state.set_state(ManualGrantFlow.confirmation)
    await message.answer(
        f"Подтвердите ручную выдачу:\n{recipient_summary(data)}\n"
        f"Материал: {data['media_name']} ({MEDIA_LABELS[str(data['media_type'])]}).",
        reply_markup=manual_confirmation_keyboard(),
    )


@router.message(Command("admin"))
async def open_admin(message: Message) -> None:
    if not is_admin(message.from_user):
        user_id = message.from_user.id if message.from_user else "unknown"
        username = message.from_user.username if message.from_user else None
        logger.warning(
            "Admin panel access denied: user_id=%s username=%s",
            user_id,
            username,
        )
        await message.answer(
            f"Доступ запрещён. Ваш user_id: {user_id}; "
            "доступ разрешён только @serbachova."
        )
        return
    await message.answer("Панель управления:", reply_markup=admin_menu())


@router.message(ManualGrantFlow.recipient)
async def receive_manual_recipient(message: Message, state: FSMContext, db: Database) -> None:
    identifier = (message.text or "").strip()
    if not identifier:
        await message.answer("Введите @username или числовой user_id.")
        return

    logger.info(
        "Manual delivery recipient lookup: admin_id=%s identifier=%s",
        message.from_user.id,
        identifier,
    )
    recipient = await db.find_user(identifier)
    if recipient is None:
        logger.info(
            "Manual delivery recipient not found: admin_id=%s identifier=%s",
            message.from_user.id,
            identifier,
        )
        await state.clear()
        await message.answer(
            "Пользователь не найден в базе. Отправить контент можно только тем, "
            "кто запускал бота (/start)."
        )
        return
    logger.info(
        "Manual delivery recipient found: admin_id=%s recipient_id=%s username=%s",
        message.from_user.id,
        recipient["user_id"],
        recipient["username"],
    )

    await state.update_data(
        recipient_id=recipient["user_id"],
        recipient_username=recipient["username"],
        recipient_first_name=recipient["first_name"],
        recipient_last_name=recipient["last_name"],
    )
    await state.set_state(ManualGrantFlow.content)
    data = await state.get_data()
    await message.answer(
        f"Получатель найден:\n{recipient_summary(data)}\n\nВыберите источник контента:",
        reply_markup=manual_content_keyboard(),
    )


async def show_catalog_page(
    callback: CallbackQuery, state: FSMContext, db: Database, page: int
) -> None:
    items = await db.active_media()
    if not items:
        await callback.message.edit_text(
            "Каталог пуст. Можно загрузить фото или видео напрямую.",
            reply_markup=manual_content_keyboard(),
        )
        return

    page_count = (len(items) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE
    page = min(max(page, 0), page_count - 1)
    start_index = page * CATALOG_PAGE_SIZE
    page_items = items[start_index : start_index + CATALOG_PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{MEDIA_LABELS[item['media_type']]} — {Path(item['file_path']).name}"[:60],
                callback_data=f"admin:manual:item:{item['id']}",
            )
        ]
        for item in page_items
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="←", callback_data=f"admin:manual:page:{page - 1}"
            )
        )
    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{page_count}", callback_data="admin:manual:catalog"
        )
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                text="→", callback_data=f"admin:manual:page:{page + 1}"
            )
        )
    rows.append(navigation)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="Загрузить своё фото/видео",
                    callback_data="admin:manual:upload",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="admin:manual:cancel")],
        ]
    )
    await state.set_state(ManualGrantFlow.content)
    await callback.message.edit_text(
        f"Выберите позицию каталога ({start_index + 1}–"
        f"{start_index + len(page_items)} из {len(items)}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def select_catalog_item(
    callback: CallbackQuery, state: FSMContext, db: Database, media_id: str
) -> None:
    item = await db.get_media(media_id)
    if item is None or not item["is_active"]:
        await callback.answer("Эта позиция больше недоступна.", show_alert=True)
        return
    file_path = Path(item["file_path"])
    file_id = item["telegram_file_id"]
    if not file_id and not file_path.is_file():
        await callback.answer("Файл отсутствует на сервере.", show_alert=True)
        return

    await state.update_data(
        media_id=item["id"],
        media_type=item["media_type"],
        media_name=file_path.name,
        source_type="file_id" if file_id else "path",
        source_value=file_id if file_id else str(file_path),
        file_path=str(file_path),
    )
    await show_manual_confirmation(callback.message, state)


@router.message(ManualGrantFlow.upload, F.photo | F.video)
async def receive_manual_media(message: Message, state: FSMContext) -> None:
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    else:
        await message.answer("Поддерживаются только фото и видео.")
        return
    await state.update_data(
        media_id=None,
        media_type=media_type,
        media_name="Файл, загруженный администратором",
        source_type="file_id",
        source_value=file_id,
        file_path=None,
        media_description=message.caption if media_type == "video" else None,
    )
    await show_manual_confirmation(message, state)


@router.message(ManualGrantFlow.upload)
async def reject_manual_media(message: Message) -> None:
    await message.answer("Пришлите фото или видео либо нажмите «Отмена».")


async def send_manual_media_once(
    bot: Bot,
    recipient_id: int,
    media_type: str,
    source_type: str,
    source_value: str,
    description: str | None = None,
) -> tuple[Message, str | None]:
    if source_type == "path":
        source: str | FSInputFile = FSInputFile(source_value)
    else:
        source = source_value

    if media_type == "photo":
        sent = await bot.send_photo(
            recipient_id, photo=source, caption="Вам открыт доступ к контенту!"
        )
        file_id = sent.photo[-1].file_id if sent.photo else None
    else:
        sent = await send_video_with_retry(
            bot,
            recipient_id,
            source,
            description="\n\n".join(
                part
                for part in ("Вам открыт доступ к контенту!", description)
                if part
            ),
        )
        file_id = sent.video.file_id if sent.video else None
    return sent, file_id


def manual_delivery_error_reason(error: Exception) -> str:
    if isinstance(error, TelegramForbiddenError):
        return "пользователь заблокировал бота или запретил сообщения"
    if isinstance(error, TelegramNotFound):
        return "чат или пользователь не найден"
    if isinstance(error, TelegramRetryAfter):
        return f"ограничение Telegram (FloodWait {error.retry_after} сек.)"
    if isinstance(error, (TelegramNetworkError, TimeoutError)):
        return "сетевая ошибка или таймаут Telegram API"
    if isinstance(error, TelegramServerError):
        return "временная ошибка сервера Telegram"
    if isinstance(error, TelegramBadRequest):
        return "Telegram отклонил медиа или параметры отправки"
    if isinstance(error, TelegramAPIError):
        return "ошибка Telegram API"
    if isinstance(error, OSError):
        return "не удалось прочитать исходный файл"
    return "непредвиденная ошибка"


async def deliver_manual_content(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    data = await state.get_data()
    recipient_id = data.get("recipient_id")
    media_type = data.get("media_type")
    source_type = data.get("source_type")
    source_value = data.get("source_value")
    if (
        not isinstance(recipient_id, int)
        or media_type not in MEDIA_LABELS
        or source_type not in {"file_id", "path"}
        or not isinstance(source_value, str)
        or not source_value
    ):
        await state.clear()
        await callback.message.edit_text(
            "Данные выдачи потеряны. Начните сценарий заново.",
            reply_markup=back_keyboard(),
        )
        return

    if await state.get_state() != ManualGrantFlow.confirmation.state:
        await callback.answer("Эта выдача уже обработана или устарела.", show_alert=True)
        return
    await state.set_state(ManualGrantFlow.sending)
    media_id = data.get("media_id")
    cached_file_id = source_value if source_type == "file_id" else None
    logger.info(
        "Manual delivery lookup complete: admin_id=%s recipient_id=%s media_id=%s "
        "source=%s",
        callback.from_user.id,
        recipient_id,
        media_id,
        source_type,
    )

    delivered: Message | None = None
    delivered_file_id: str | None = None
    final_error: Exception | None = None
    max_attempts = 1 if media_type == "video" else 2
    try:
        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Manual delivery attempt %s/%s: admin_id=%s recipient_id=%s media_id=%s",
                attempt,
                max_attempts,
                callback.from_user.id,
                recipient_id,
                media_id,
            )
            try:
                media_description = data.get("media_description")
                if (
                    media_type == "video"
                    and isinstance(media_id, str)
                    and isinstance(data.get("file_path"), str)
                ):
                    description = "Вам открыт доступ к контенту!"
                    if isinstance(media_description, str) and media_description.strip():
                        description += f"\n\n{media_description.strip()}"
                    delivered = await send_catalog_video(
                        bot=bot,
                        chat_id=recipient_id,
                        media_id=media_id,
                        file_path=data["file_path"],
                        cached_file_id=(
                            str(source_value) if source_type == "file_id" else None
                        ),
                        update_file_id=lambda file_id: db.set_telegram_file_id(
                            media_id, file_id
                        ),
                        description=description,
                    )
                    delivered_file_id = (
                        delivered.video.file_id if delivered.video else None
                    )
                else:
                    delivered, delivered_file_id = await send_manual_media_once(
                        bot,
                        recipient_id,
                        str(media_type),
                        str(source_type),
                        str(source_value),
                        media_description
                        if isinstance(media_description, str)
                        else None,
                    )
                logger.info(
                    "Manual delivery attempt %s succeeded: recipient_id=%s media_id=%s",
                    attempt,
                    recipient_id,
                    media_id,
                )
                break
            except TelegramRetryAfter as error:
                final_error = error
                logger.warning(
                    "Manual delivery FloodWait: attempt=%s recipient_id=%s "
                    "retry_after=%s",
                    attempt,
                    recipient_id,
                    error.retry_after,
                )
                if attempt == max_attempts:
                    break
                await asyncio.sleep(error.retry_after)
            except (TelegramNetworkError, TelegramServerError, TimeoutError) as error:
                final_error = error
                logger.warning(
                    "Manual delivery transient error: attempt=%s recipient_id=%s "
                    "error=%s",
                    attempt,
                    recipient_id,
                    error,
                    exc_info=True,
                )
                if attempt == max_attempts:
                    break
                await asyncio.sleep(1.5)
            except Exception as error:
                final_error = error
                logger.exception(
                    "Manual delivery non-retryable error: recipient_id=%s media_id=%s",
                    recipient_id,
                    media_id,
                )
                break

        if delivered is not None:
            cache_saved = True
            if media_id and source_type == "path" and delivered_file_id:
                cached_file_id = delivered_file_id
                try:
                    await db.set_telegram_file_id(str(media_id), delivered_file_id)
                except aiosqlite.Error:
                    cache_saved = False
                    logger.exception(
                        "Media delivered but file_id cache could not be saved: media_id=%s",
                        media_id,
                    )
                else:
                    logger.info(
                        "Cached Telegram file_id after first manual delivery: media_id=%s",
                        media_id,
                    )
            status = "выдано вручную администратором"
            grant_status = status
            admin_result = (
                f"Контент успешно доставлен пользователю {recipient_id}."
            )
            if not cache_saved:
                admin_result += "\nВнимание: file_id не сохранился в каталоге БД."
            logger.info(
                "Manual delivery completed: admin_id=%s recipient_id=%s media_id=%s",
                callback.from_user.id,
                recipient_id,
                media_id,
            )
        else:
            error = final_error or RuntimeError("Unknown delivery failure")
            reason = manual_delivery_error_reason(error)
            status = f"ошибка доставки: {reason}"
            grant_status = status
            admin_result = (
                f"{MANUAL_DELIVERY_FAILURE_MESSAGE}\n\n"
                f"Получатель: {recipient_id}\nПричина: {reason}\n"
                f"Детали: {str(error)[:500]}"
            )
            logger.error(
                "Manual delivery failed after retry policy: admin_id=%s "
                "recipient_id=%s media_id=%s reason=%s",
                callback.from_user.id,
                recipient_id,
                media_id,
                reason,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
            )
            try:
                await bot.send_message(recipient_id, RECIPIENT_DELIVERY_FAILURE_MESSAGE)
                logger.info(
                    "Manual delivery failure notice sent to recipient_id=%s",
                    recipient_id,
                )
            except TelegramAPIError:
                logger.warning(
                    "Could not send manual delivery failure notice: recipient_id=%s",
                    recipient_id,
                    exc_info=True,
                )

        try:
            await db.record_manual_grant(
                user_id=recipient_id,
                admin_id=callback.from_user.id,
                media_id=media_id if isinstance(media_id, str) else None,
                media_type=str(media_type),
                media_name=str(data.get("media_name", "медиафайл")),
                telegram_file_id=cached_file_id,
                status=grant_status,
            )
        except aiosqlite.Error:
            logger.exception(
                "Could not record manual delivery: recipient_id=%s media_id=%s",
                recipient_id,
                media_id,
            )
            if delivered is not None:
                admin_result += "\nНе удалось записать выдачу в базу; проверьте лог."

        try:
            await callback.message.edit_text(
                admin_result, reply_markup=back_keyboard()
            )
        except TelegramAPIError:
            logger.exception(
                "Could not notify admin about manual delivery: admin_id=%s",
                callback.from_user.id,
            )
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("admin:"))
async def admin_actions(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    state: FSMContext,
) -> None:
    if not is_admin(callback.from_user):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    action = callback.data.removeprefix("admin:")
    await callback.answer()

    if action == "home":
        await state.clear()
        await callback.message.edit_text("Панель управления:", reply_markup=admin_menu())
    elif action == "manual":
        await state.clear()
        await state.set_state(ManualGrantFlow.recipient)
        await callback.message.edit_text(
            "Отправьте @username или числовой user_id получателя.",
            reply_markup=manual_cancel_keyboard(),
        )
    elif action == "manual:upload":
        await state.set_state(ManualGrantFlow.upload)
        await callback.message.edit_text(
            "Отправьте боту фото или видео, которое нужно выдать.",
            reply_markup=manual_cancel_keyboard(),
        )
    elif action == "manual:catalog":
        await show_catalog_page(callback, state, db, 0)
    elif action.startswith("manual:page:"):
        try:
            page = int(action.rsplit(":", maxsplit=1)[1])
        except ValueError:
            await callback.answer("Некорректная страница.", show_alert=True)
            return
        await show_catalog_page(callback, state, db, page)
    elif action.startswith("manual:item:"):
        await select_catalog_item(
            callback, state, db, action.split(":", maxsplit=2)[2]
        )
    elif action == "manual:confirm":
        await deliver_manual_content(callback, state, db, bot)
    elif action == "manual:cancel":
        await state.clear()
        await callback.message.edit_text(
            "Выдача отменена.", reply_markup=back_keyboard()
        )
    elif action == "health":
        await callback.message.edit_text(
            await build_health_report(db), reply_markup=back_keyboard()
        )
    elif action == "tests":
        await callback.message.edit_text(
            "Выберите файл для проверки отправки:", reply_markup=tests_menu()
        )
    elif action == "stats":
        await callback.message.edit_text(
            await build_statistics_report(db), reply_markup=back_keyboard()
        )
    elif action == "rescan":
        await rescan_catalog(callback, db)
    elif action == "warm":
        await prewarm_cache(callback, db, bot)
    elif action.startswith("send:"):
        await send_test_media(callback, db, bot, action.split(":", maxsplit=1)[1])
    else:
        await callback.message.edit_text(
            "Неизвестное действие.", reply_markup=admin_menu()
        )


async def build_health_report(db: Database) -> str:
    try:
        summary = await db.health_summary()
        registered = await db.active_media()
        disk_items = scan_media()
    except Exception:
        logger.exception("Admin health check failed")
        return "Проверка системы завершилась ошибкой. Подробности записаны в лог."

    total_bytes = 0
    readable = 0
    unreadable_names: list[str] = []
    for item in disk_items:
        path = Path(item.file_path)
        try:
            total_bytes += path.stat().st_size
            with path.open("rb") as file:
                file.read(1)
            readable += 1
        except OSError:
            unreadable_names.append(path.name)

    disk_paths = {os.path.normcase(os.path.abspath(item.file_path)) for item in disk_items}
    registered_paths = {
        os.path.normcase(os.path.abspath(row["file_path"])) for row in registered
    }
    broken_paths = [
        row["file_path"] for row in registered if not Path(row["file_path"]).is_file()
    ]
    unregistered = disk_paths - registered_paths
    photos = sum(item.media_type == "photo" for item in disk_items)
    videos = sum(item.media_type == "video" for item in disk_items)
    integrity = summary["integrity"]

    lines = [
        "Проверка системы",
        f"SQLite integrity_check: {integrity}",
        f"Папка ggfiles: {'доступна' if os.access(MEDIA_DIR, os.R_OK) else 'нет доступа на чтение'}",
        f"Файлы на диске: {len(disk_items)} (фото {photos}, видео {videos}), "
        f"размер {total_bytes / (1024 * 1024):.1f} MiB",
        f"Чтение файлов: {readable}/{len(disk_items)} доступны",
        f"Каталог БД: {len(registered)} активных, не зарегистрировано {len(unregistered)}, "
        f"битых путей {len(broken_paths)}",
        f"Покупки: {summary['purchase_count']}; заработано {summary['stars_earned']} XTR",
    ]
    if unreadable_names:
        lines.append("Не читаются: " + ", ".join(unreadable_names[:5]))
    if broken_paths:
        lines.append("Отсутствуют: " + ", ".join(Path(path).name for path in broken_paths[:5]))
    return "\n".join(lines)


async def build_statistics_report(db: Database) -> str:
    try:
        summary = await db.health_summary()
        transactions = await db.recent_purchases(5)
    except Exception:
        logger.exception("Admin statistics query failed")
        return "Не удалось получить статистику. Подробности записаны в лог."

    lines = [
        "Статистика покупок",
        f"Уникальных покупателей: {summary['buyer_count']}",
        f"Всего покупок: {summary['purchase_count']}",
        f"Заработано: {summary['stars_earned']} XTR",
        "Последние транзакции:",
    ]
    if not transactions:
        lines.append("Покупок пока нет.")
    for transaction in transactions:
        if transaction["media_type"] == "package":
            title = PACKAGE_LABELS.get(transaction["media_id"], transaction["media_id"])
            item = f"{title} ({transaction['price']} XTR)"
        else:
            item = (
                f"{transaction['media_id']} "
                f"({MEDIA_LABELS[transaction['media_type']]}, "
                f"{transaction['price']} XTR)"
            )
        lines.append(
            f"• user {transaction['user_id']} — {item}, "
            f"{transaction['purchased_at']}"
        )
    return "\n".join(lines)


async def rescan_catalog(callback: CallbackQuery, db: Database) -> None:
    try:
        items = scan_media()
        await db.refresh_catalog(items)
        photos = sum(item.media_type == "photo" for item in items)
        videos = sum(item.media_type == "video" for item in items)
        text = (
            "Каталог пересканирован.\n"
            f"Всего файлов: {len(items)} (фото {photos}, видео {videos}).\n"
            "История покупок сохранена."
        )
    except Exception:
        logger.exception("Admin catalog rescan failed")
        text = "Не удалось пересканировать каталог. Подробности записаны в лог."
    await callback.message.edit_text(text, reply_markup=back_keyboard())


async def prewarm_cache(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    storage_channel = os.getenv("STORAGE_CHANNEL_ID", "").strip()
    if not storage_channel:
        await callback.message.edit_text(
            "Предзагрузка не настроена: укажите STORAGE_CHANNEL_ID в .env "
            "и добавьте бота в канал с правом публикации.",
            reply_markup=back_keyboard(),
        )
        return
    try:
        storage_chat_id: int | str = (
            int(storage_channel) if storage_channel.lstrip("-").isdigit() else storage_channel
        )
        await db.refresh_catalog(scan_media())
        items = await db.uncached_media()
    except Exception:
        logger.exception("Could not prepare media cache pre-warm")
        await callback.message.edit_text(
            "Не удалось подготовить предзагрузку. Подробности записаны в лог.",
            reply_markup=back_keyboard(),
        )
        return

    if not items:
        await callback.message.edit_text(
            "Кэш уже прогрет: все активные файлы имеют Telegram file_id.",
            reply_markup=back_keyboard(),
        )
        return

    await callback.message.edit_text(
        f"Предзагрузка началась: 0/{len(items)} файлов."
    )
    uploaded = 0
    failed: list[str] = []
    for index, item in enumerate(items, start=1):
        path = Path(item["file_path"])
        try:
            if not path.is_file():
                raise FileNotFoundError(path)
            for attempt in range(4):
                try:
                    upload = FSInputFile(path)
                    if item["media_type"] == "photo":
                        sent = await bot.send_photo(storage_chat_id, photo=upload)
                        file_id = sent.photo[-1].file_id if sent.photo else None
                    else:
                        sent = await send_catalog_video(
                            bot=bot,
                            chat_id=storage_chat_id,
                            media_id=item["id"],
                            file_path=path,
                            cached_file_id=None,
                            update_file_id=lambda file_id: db.set_telegram_file_id(
                                item["id"], file_id
                            ),
                        )
                        file_id = sent.video.file_id if sent.video else None
                    break
                except TelegramRetryAfter as error:
                    if attempt == 3:
                        raise
                    logger.warning(
                        "Telegram rate limited cache pre-warm; retrying after %s seconds",
                        error.retry_after,
                    )
                    await asyncio.sleep(error.retry_after)
            if not file_id:
                raise RuntimeError("Telegram response did not contain a file_id")
            await db.set_telegram_file_id(item["id"], file_id)
            uploaded += 1
        except (OSError, TelegramAPIError, RuntimeError):
            logger.exception(
                "Cache pre-warm failed for media_id=%s path=%s",
                item["id"],
                path,
            )
            failed.append(path.name)

        if index % 10 == 0 or index == len(items):
            try:
                await callback.message.edit_text(
                    f"Предзагрузка: {index}/{len(items)}; "
                    f"успешно {uploaded}, ошибок {len(failed)}."
                )
            except TelegramAPIError:
                logger.exception("Could not update cache pre-warm progress")

    result = (
        f"Предзагрузка завершена.\nУспешно: {uploaded}/{len(items)}."
        f"\nОшибок: {len(failed)}."
    )
    if failed:
        result += "\nПроблемные файлы: " + ", ".join(failed[:10])
    await callback.message.edit_text(result, reply_markup=back_keyboard())


async def send_test_media(
    callback: CallbackQuery, db: Database, bot: Bot, media_type: str
) -> None:
    if media_type not in MEDIA_LABELS:
        await callback.message.answer("Неизвестный тип медиа.")
        return
    items = await db.active_media(media_type)
    available = [
        item
        for item in items
        if item["telegram_file_id"] or Path(item["file_path"]).is_file()
    ]
    if not available:
        await callback.message.answer(
            f"В каталоге нет доступных файлов: {MEDIA_LABELS[media_type].lower()}."
        )
        return

    item = random.choice(available)
    file_path = Path(item["file_path"])
    try:
        upload = FSInputFile(file_path)
        if media_type == "photo":
            await bot.send_photo(
                chat_id=callback.from_user.id,
                photo=upload,
                caption=f"Тестовая отправка: {file_path.name}",
            )
        else:
            await send_catalog_video(
                bot=bot,
                chat_id=callback.from_user.id,
                media_id=item["id"],
                file_path=file_path,
                cached_file_id=item["telegram_file_id"],
                update_file_id=lambda file_id: db.set_telegram_file_id(
                    item["id"], file_id
                ),
                description=f"Тестовая отправка: {file_path.name}",
            )
        await callback.message.answer(f"Тест успешно отправлен: {file_path.name}")
    except (OSError, TelegramAPIError):
        logger.exception("Test media delivery failed: %s", file_path)
        await callback.message.answer(
            f"Не удалось отправить файл «{file_path.name}». "
            "Возможны повреждение файла, неподдерживаемый формат/кодек "
            "или превышение лимита Telegram."
        )
