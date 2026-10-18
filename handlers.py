import logging
from pathlib import Path
from uuid import uuid4

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
)

from admin import is_admin
from database import Database, MediaItem
from video_delivery import send_catalog_video


logger = logging.getLogger(__name__)
router = Router()
PRICE_BY_TYPE = {"photo": 10, "video": 20}
TITLE_BY_TYPE = {"photo": "Фото", "video": "Видео"}
EMPTY_CATEGORY_MESSAGE = "Вы уже приобрели весь доступный контент из этой категории!"
SUPPORT_CONTACT = "@serbachova"
SUPPORT_URL = "https://t.me/serbachova"
MEGA_DELIVERY_URL = "https://mega.nz/folder/v5pCRYiI#Dpi3HIygR6yr8HSEPJUqug"
PACKAGES = {
    "archive": {
        "title": "Базовый архив",
        "description": "Полный доступ к основной базе файлов.",
        "price": 1_000,
    },
    "full": {
        "title": "FULL PACK — 550 видео",
        "description": "Премиум-коллекция из 550 отборных видео без ограничений.",
        "price": 2_500,
    },
}


def catalog_keyboard(show_admin: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
            [
                InlineKeyboardButton(text="Купить фото — 10 Stars", callback_data="buy:photo"),
                InlineKeyboardButton(text="Купить видео — 20 Stars", callback_data="buy:video"),
            ],
            [
                InlineKeyboardButton(
                    text="Архив — 1 000 ⭐", callback_data="pack:archive"
                ),
                InlineKeyboardButton(
                    text="FULL PACK — 2 500 ⭐", callback_data="pack:full"
                ),
            ],
        ]
    if show_admin:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="⚙️ Админ-панель", callback_data="admin:home"
                )
            ]
        )
    keyboard.append(
        [InlineKeyboardButton(text="Поддержка — @serbachova", url=SUPPORT_URL)]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Добро пожаловать в магазин цифровых медиа!\n"
        "Выберите фото или видео для покупки. Каждый файл можно купить только один раз.\n"
        "Также доступны базовый архив за <b>1 000 ⭐</b> и FULL PACK из "
        "<b>550 видео за 2 500 ⭐</b>.\n"
        "История покупок: /purchases.\n"
        f"По вопросам, заказам и технической помощи: {SUPPORT_CONTACT}.",
        parse_mode="HTML",
        reply_markup=catalog_keyboard(
            is_admin(message.from_user) if message.from_user else False
        ),
    )


@router.message(Command("purchases"))
async def purchases(message: Message, db: Database) -> None:
    if message.from_user is None:
        return
    history = await db.purchase_history(message.from_user.id)
    if not history:
        lines = ["Ваши покупки:"]
    else:
        lines = ["Ваши покупки:"]
    for item in history:
        filename = Path(item["file_path"]).name
        lines.append(
            f"• {TITLE_BY_TYPE[item['media_type']]}: {filename} "
            f"— {item['purchased_at']}"
        )
    package_history = await db.package_purchase_history(message.from_user.id)
    for item in package_history:
        package = PACKAGES.get(item["product_id"])
        title = package["title"] if package else item["product_id"]
        lines.append(
            f"• {title} — {item['price']} ⭐ — {item['purchased_at']}"
        )
        lines.append(f"  Ссылка для получения: {MEGA_DELIVERY_URL}")
    if len(lines) == 1:
        await message.answer("У вас пока нет покупок.")
        return
    await message.answer("\n".join(lines))


def package_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Купить архив — 1 000 ⭐",
                    callback_data="pack_checkout:archive",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Взять FULL PACK — 2 500 ⭐",
                    callback_data="pack_checkout:full",
                )
            ],
        ]
    )


async def send_package_invoice(
    callback: CallbackQuery, db: Database, product_id: str
) -> None:
    product = PACKAGES[product_id]
    order_id = uuid4().hex
    payload = f"pack:{order_id}"
    await db.create_package_order(
        order_id=order_id,
        user_id=callback.from_user.id,
        product_id=product_id,
        price=product["price"],
    )
    try:
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title=product["title"],
            description=product["description"],
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=product["title"], amount=product["price"])],
        )
    except TelegramAPIError:
        logger.exception(
            "Could not send package invoice: user_id=%s product_id=%s",
            callback.from_user.id,
            product_id,
        )
        await db.delete_pending_package_order(order_id)
        await callback.message.answer(
            f"Не удалось создать счёт. Попробуйте позже или напишите {SUPPORT_CONTACT}."
        )


@router.callback_query(F.data.startswith("pack:"))
async def buy_package(callback: CallbackQuery, db: Database) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    product_id = callback.data.removeprefix("pack:")
    if product_id == "archive":
        await callback.answer()
        await callback.message.answer(
            "Кстати, у нас также есть закрытый FULL PACK на <b>550 видео</b> всего "
            "за <b>2 500 ⭐</b>. В него входит <b>максимальный объём контента без "
            "урезаний</b>. Хотите сразу взять его?",
            parse_mode="HTML",
            reply_markup=package_keyboard(),
        )
        return
    if product_id not in PACKAGES:
        await callback.answer("Неизвестный тариф.", show_alert=True)
        return
    await callback.answer()
    await send_package_invoice(callback, db, product_id)


@router.callback_query(F.data.startswith("pack_checkout:"))
async def checkout_package(callback: CallbackQuery, db: Database) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    product_id = callback.data.removeprefix("pack_checkout:")
    if product_id not in PACKAGES:
        await callback.answer("Неизвестный тариф.", show_alert=True)
        return
    await callback.answer()
    await send_package_invoice(callback, db, product_id)


@router.callback_query(F.data.startswith("buy:"))
async def buy_category(callback: CallbackQuery, db: Database) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    _, media_type = callback.data.split(":", maxsplit=1)
    if media_type not in PRICE_BY_TYPE:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return

    media, already_reserved = await db.reserve_next(callback.from_user.id, media_type)
    if media is None:
        await callback.answer(
            f"{EMPTY_CATEGORY_MESSAGE} По вопросам заказа: {SUPPORT_CONTACT}.",
            show_alert=True,
        )
        return

    file_path = Path(media["file_path"])
    if not file_path.is_file():
        logger.error("Catalog file is missing: %s (media_id=%s)", file_path, media["id"])
        await db.release_reservation(callback.from_user.id, media["id"])
        await callback.answer(
            f"Файл временно недоступен. Попробуйте позже или напишите {SUPPORT_CONTACT}.",
            show_alert=True,
        )
        return

    if already_reserved:
        await callback.answer(
            "У вас уже есть неоплаченный счёт на этот файл. "
            f"Оплатите его в течение 30 минут или попробуйте позже. Вопросы по заказу: {SUPPORT_CONTACT}.",
            show_alert=True,
        )
        return

    title = TITLE_BY_TYPE[media_type]
    try:
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"{title} из каталога",
            description=f"Покупка одного файла категории «{title.lower()}».",
            payload=media["id"],
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=PRICE_BY_TYPE[media_type])],
        )
        await callback.answer()
    except TelegramAPIError:
        logger.exception("Could not send invoice for media_id=%s", media["id"])
        await db.release_reservation(callback.from_user.id, media["id"])
        await callback.answer(
            f"Не удалось создать счёт. Попробуйте позже или напишите {SUPPORT_CONTACT}.",
            show_alert=True,
        )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    if query.invoice_payload.startswith("pack:"):
        order_id = query.invoice_payload.removeprefix("pack:")
        order = await db.get_package_order(order_id)
        product = PACKAGES.get(order["product_id"]) if order is not None else None
        valid = (
            order is not None
            and product is not None
            and order["user_id"] == query.from_user.id
            and order["status"] == "pending"
            and order["price"] == product["price"]
            and query.currency == "XTR"
            and query.total_amount == product["price"]
        )
        if not valid:
            await query.answer(
                ok=False,
                error_message="Данные тарифа не совпадают или заказ уже обработан. Создайте новый счёт.",
            )
            return
        await query.answer(ok=True)
        return

    media = await db.get_checkout_media(query.from_user.id, query.invoice_payload)
    if media is None:
        await query.answer(
            ok=False,
            error_message="Заказ истёк или уже оплачен. Вернитесь в каталог и создайте новый счёт.",
        )
        return

    file_path = Path(media["file_path"])
    expected_price = PRICE_BY_TYPE.get(media["media_type"])
    if (
        not file_path.is_file()
        or expected_price is None
        or media["price"] != expected_price
        or query.currency != "XTR"
        or query.total_amount != expected_price
    ):
        if not file_path.is_file():
            logger.error("Media file disappeared before checkout: %s", file_path)
        await query.answer(
            ok=False,
            error_message="Файл или данные заказа недоступны. Создайте новый счёт.",
        )
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, db: Database, bot: Bot) -> None:
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return

    if payment.invoice_payload.startswith("pack:"):
        await successful_package_payment(message, db, payment)
        return

    media: MediaItem | None = None
    row = await db.get_media(payment.invoice_payload)
    if row is not None:
        media = MediaItem(
            id=row["id"],
            file_path=row["file_path"],
            media_type=row["media_type"],
            price=row["price"],
        )
    telegram_file_id = row["telegram_file_id"] if row is not None else None
    expected_price = PRICE_BY_TYPE.get(media.media_type) if media else None
    if (
        media is None
        or expected_price is None
        or media.price != expected_price
        or payment.currency != "XTR"
        or payment.total_amount != expected_price
    ):
        logger.error(
            "Successful payment has invalid product details: user_id=%s media_id=%s",
            message.from_user.id,
            payment.invoice_payload,
        )
        await message.answer(
            f"Платёж получен, но заказ не удалось проверить. Напишите {SUPPORT_CONTACT}."
        )
        return

    recorded = await db.record_purchase(
        user_id=message.from_user.id,
        media_id=media.id,
        payment_charge_id=payment.telegram_payment_charge_id,
    )
    if not recorded:
        logger.error(
            "Duplicate purchase/payment update: user_id=%s media_id=%s charge_id=%s",
            message.from_user.id,
            media.id,
            payment.telegram_payment_charge_id,
        )
        await message.answer(
            "Этот файл уже зарегистрирован как покупка. Если списание повторилось, "
            f"напишите {SUPPORT_CONTACT}."
        )
        return

    file_path = Path(media.file_path)
    if not telegram_file_id and not file_path.is_file():
        logger.error(
            "Purchased media is missing: user_id=%s media_id=%s path=%s",
            message.from_user.id,
            media.id,
            file_path,
        )
        await message.answer(
            f"Платёж получен, но файл не найден на сервере. Напишите {SUPPORT_CONTACT}."
        )
        return

    try:
        if media.media_type == "video":
            await send_catalog_video(
                bot=bot,
                chat_id=message.chat.id,
                media_id=media.id,
                file_path=file_path,
                cached_file_id=telegram_file_id,
                update_file_id=lambda file_id: db.set_telegram_file_id(
                    media.id, file_id
                ),
                description="Спасибо за покупку! Ваше видео:",
            )
            return

        if telegram_file_id:
            try:
                await message.answer_photo(
                    telegram_file_id, caption="Спасибо за покупку! Ваше фото:"
                )
                return
            except TelegramBadRequest:
                logger.warning(
                    "Cached Telegram file_id was rejected; re-uploading media_id=%s",
                    media.id,
                    exc_info=True,
                )
                await db.set_telegram_file_id(media.id, None)
                if not file_path.is_file():
                    raise FileNotFoundError(file_path)

        upload = FSInputFile(file_path)
        sent_message = await message.answer_photo(
            upload, caption="Спасибо за покупку! Ваше фото:"
        )
        uploaded_file_id = sent_message.photo[-1].file_id if sent_message.photo else None
        if uploaded_file_id:
            await db.set_telegram_file_id(media.id, uploaded_file_id)
    except TelegramForbiddenError:
        logger.warning(
            "Purchased media delivery blocked by recipient: user_id=%s media_id=%s",
            message.from_user.id,
            media.id,
        )
    except (OSError, TelegramAPIError, TimeoutError):
        logger.exception(
            "Could not deliver purchased file: user_id=%s media_id=%s",
            message.from_user.id,
            media.id,
        )
        try:
            await message.answer(
                f"Платёж получен, но файл не удалось отправить. Напишите {SUPPORT_CONTACT}."
            )
        except TelegramAPIError:
            logger.warning(
                "Could not notify buyer about delivery failure: user_id=%s",
                message.from_user.id,
                exc_info=True,
            )


async def successful_package_payment(
    message: Message, db: Database, payment: SuccessfulPayment
) -> None:
    if message.from_user is None:
        return
    order_id = payment.invoice_payload.removeprefix("pack:")
    order = await db.get_package_order(order_id)
    product = PACKAGES.get(order["product_id"]) if order is not None else None
    if (
        order is None
        or product is None
        or order["user_id"] != message.from_user.id
        or order["price"] != product["price"]
        or payment.currency != "XTR"
        or payment.total_amount != product["price"]
    ):
        logger.error(
            "Successful package payment did not match order: user_id=%s order_id=%s",
            message.from_user.id,
            order_id,
        )
        await message.answer(
            f"Платёж получен, но тариф не удалось проверить. Напишите {SUPPORT_CONTACT}."
        )
        return

    try:
        recorded = await db.record_package_payment(
            order_id=order_id,
            user_id=message.from_user.id,
            payment_charge_id=payment.telegram_payment_charge_id,
        )
    except aiosqlite.Error:
        logger.exception(
            "Could not record paid package order: user_id=%s order_id=%s",
            message.from_user.id,
            order_id,
        )
        await message.answer(
            f"Платёж получен, но выдачу не удалось зарегистрировать. Напишите {SUPPORT_CONTACT}."
        )
        return
    if not recorded:
        logger.error(
            "Package payment charge was already used by another order: user_id=%s "
            "order_id=%s charge_id=%s",
            message.from_user.id,
            order_id,
            payment.telegram_payment_charge_id,
        )
        await message.answer(
            f"Не удалось подтвердить этот заказ. Напишите {SUPPORT_CONTACT}."
        )
        return

    logger.info(
        "Package payment confirmed: user_id=%s product_id=%s order_id=%s",
        message.from_user.id,
        order["product_id"],
        order_id,
    )
    try:
        await message.answer(
            f"Оплата тарифа «<b>{product['title']}</b>» на "
            f"<b>{product['price']} ⭐</b> подтверждена!\n"
            f"Ссылка для получения: {MEGA_DELIVERY_URL}",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        logger.exception(
            "Package paid but delivery link could not be sent: user_id=%s "
            "product_id=%s order_id=%s",
            message.from_user.id,
            order["product_id"],
            order_id,
        )


@router.errors()
async def global_error_handler(event: ErrorEvent, bot: Bot) -> bool:
    exception = event.exception
    if isinstance(exception, (TelegramAPIError, TelegramBadRequest, FileNotFoundError)):
        logger.error(
            "Handled bot/API/file error",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
    else:
        logger.error(
            "Unhandled exception in update",
            exc_info=(type(exception), exception, exception.__traceback__),
        )

    update = event.update
    message = update.message or update.edited_message
    callback = update.callback_query
    chat_id = message.chat.id if message else (
        callback.message.chat.id if callback and callback.message else None
    )
    if chat_id is not None and not isinstance(exception, TelegramAPIError):
        try:
            await bot.send_message(
                chat_id,
                f"Произошла внутренняя ошибка. Попробуйте ещё раз или напишите {SUPPORT_CONTACT}.",
            )
        except TelegramAPIError:
            logger.exception("Could not send global error notification")
    return True
