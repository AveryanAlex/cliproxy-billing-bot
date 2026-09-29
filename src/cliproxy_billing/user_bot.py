from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .keeper import KeeperClient, KeeperError
from .ledger import LedgerError, get_balance, link_key, submit_payment, upsert_user
from .models import Payment, User
from .money import money_text, parse_minor, suggested_payment_minor
from .screens import show_balance, show_history, show_keys, show_payment_start
from .ui import (
    PAY_SUGGESTED_PREFIX,
    RUB_BUTTON,
    USD_BUTTON,
    balance_text,
    buttons,
    cancel_keyboard,
    currency_keyboard,
    main_keyboard,
    payment_choice_keyboard,
    suggested_payment_button,
    suggested_payment_text,
)

logger = logging.getLogger(__name__)


class Linking(StatesGroup):
    key = State()


class Paying(StatesGroup):
    currency = State()
    choice = State()
    screenshot = State()


async def send_admin_review(
    bot: Bot, sessions: async_sessionmaker[AsyncSession], payment_id: int, admin_ids: frozenset[int]
) -> None:
    async with sessions() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            return
        user = await session.get(User, payment.user_id)
        name = user.display_name if user else str(payment.user_id)
    caption = (
        f"Проверить платёж #{payment.id}\n"
        f"{name} (Telegram ID {payment.user_id})\n"
        f"Заявлено: {money_text(payment.amount_minor, payment.currency)}"
    )
    markup = buttons(
        (
            ("✅ Подтвердить", f"review:yes:{payment.id}"),
            ("❌ Отклонить", f"review:no:{payment.id}"),
        ),
    )
    for admin_id in admin_ids:
        try:
            if payment.screenshot_kind == "photo" and payment.screenshot_file_id:
                await bot.send_photo(
                    admin_id, payment.screenshot_file_id, caption=caption, reply_markup=markup
                )
            elif payment.screenshot_kind == "document" and payment.screenshot_file_id:
                await bot.send_document(
                    admin_id, payment.screenshot_file_id, caption=caption, reply_markup=markup
                )
        except TelegramAPIError:
            logger.warning("Could not notify admin %s about payment %s", admin_id, payment.id)


def make_user_router(
    sessions: async_sessionmaker[AsyncSession],
    keeper: KeeperClient,
    admin_ids: frozenset[int],
    time_zone: ZoneInfo,
) -> Router:
    router = Router(name="users")

    @router.callback_query(F.data == "user:add_key")
    async def add_key(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        await state.clear()
        await state.set_state(Linking.key)
        await bot.send_message(
            query.from_user.id,
            "Отправьте ещё один API-ключ одним сообщением.",
            reply_markup=cancel_keyboard(),
        )

    @router.message(Linking.key, F.text)
    async def receive_key(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.chat.type != "private" or message.from_user is None or message.text is None:
            return
        supplied_key = message.text.strip()
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            logger.warning("Could not delete a submitted key message from chat %s", message.chat.id)
        if not supplied_key or len(supplied_key) > 500:
            await bot.send_message(
                message.chat.id, "Неверный формат ключа. Отправьте ключ ещё раз."
            )
            return
        try:
            key = await keeper.identify_key(supplied_key)
        except KeeperError as error:
            await bot.send_message(message.chat.id, str(error))
            return
        if key is None:
            await bot.send_message(
                message.chat.id, "Такого действующего ключа нет в Keeper. Проверьте ключ."
            )
            return
        try:
            async with sessions() as session:
                async with session.begin():
                    await upsert_user(session, message.from_user.id, message.from_user.full_name)
                    added = await link_key(session, message.from_user.id, key.id, key.label)
                balance = await get_balance(session, message.from_user.id)
        except LedgerError as error:
            await bot.send_message(message.chat.id, str(error))
            return
        await state.clear()
        action = "Ключ привязан." if added else "Этот ключ уже привязан к вам."
        await bot.send_message(
            message.chat.id,
            f"{action}\n{balance_text(balance)}",
            reply_markup=main_keyboard(is_admin=message.from_user.id in admin_ids),
        )

    @router.message(Linking.key)
    async def require_key_text(message: Message) -> None:
        if message.chat.type == "private":
            await message.answer(
                "Отправьте API-ключ текстом одним сообщением или нажмите «Отмена».",
                reply_markup=cancel_keyboard(),
            )

    @router.callback_query(F.data == "user:balance")
    async def balance_callback(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        await state.clear()
        await show_balance(
            bot, sessions, query.from_user.id, is_admin=query.from_user.id in admin_ids
        )

    @router.callback_query(F.data == "user:history")
    async def history_callback(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        await state.clear()
        await show_history(
            bot, sessions, query.from_user.id, time_zone, is_admin=query.from_user.id in admin_ids
        )

    @router.callback_query(F.data == "user:keys")
    async def keys_callback(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        await state.clear()
        await show_keys(bot, sessions, query.from_user.id, is_admin=query.from_user.id in admin_ids)

    @router.callback_query(F.data == "pay:start")
    async def pay_start(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        await state.clear()
        await state.set_state(Paying.currency)
        await show_payment_start(bot, sessions, query.from_user.id)

    async def show_amount_choice(bot: Bot, user_id: int, currency: str, error: str = "") -> None:
        async with sessions() as session:
            balance = await get_balance(session, user_id)
        due = balance.due_usd_cents if currency == "USD" else balance.due_rub_kopeks
        suggested = suggested_payment_minor(due, currency)
        if due <= 0:
            prompt = (
                f"Долга нет. Если хотите внести аванс, отправьте сумму в {currency} "
                "числом, например 100."
            )
        elif currency == "RUB":
            prompt = (
                f"Долг: {money_text(due, currency)}.\n"
                f"Для удобства предлагаем перевести {suggested_payment_text(suggested, currency)}. "
            )
            if suggested > due:
                prompt += "Разница после подтверждения зачтётся авансом на будущие начисления. "
            prompt += "Нажмите кнопку или отправьте любую сумму числом."
        else:
            prompt = (
                f"Долг: {money_text(due, currency)}.\n"
                "Нажмите кнопку или отправьте любую сумму числом."
            )
        await bot.send_message(
            user_id,
            f"{error}{prompt}",
            reply_markup=payment_choice_keyboard(amount_minor=suggested, currency=currency),
        )

    async def choose_currency(bot: Bot, state: FSMContext, user_id: int, currency: str) -> None:
        await state.update_data(currency=currency)
        await state.set_state(Paying.choice)
        await show_amount_choice(bot, user_id, currency)

    @router.message(Paying.currency, F.text.in_({USD_BUTTON, RUB_BUTTON}))
    async def pay_currency_message(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        currency = "USD" if message.text == USD_BUTTON else "RUB"
        await choose_currency(bot, state, message.from_user.id, currency)

    @router.message(Paying.currency)
    async def require_currency(message: Message) -> None:
        await message.answer(
            "Выберите USD или RUB на клавиатуре либо нажмите «Отмена».",
            reply_markup=currency_keyboard(),
        )

    @router.callback_query(F.data.startswith("pay:choose:"))
    async def pay_choose(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        currency = (query.data or "").rsplit(":", 1)[-1]
        if currency not in {"USD", "RUB"}:
            return
        await choose_currency(bot, state, query.from_user.id, currency)

    @router.message(Paying.choice)
    async def receive_payment_amount(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        currency = str(data.get("currency") or "")
        if currency not in {"USD", "RUB"}:
            await state.clear()
            await message.answer(
                "Шаг оплаты потерян. Начните заново.",
                reply_markup=main_keyboard(
                    is_admin=message.from_user is not None and message.from_user.id in admin_ids
                ),
            )
            return
        if message.from_user is None:
            return
        async with sessions() as session:
            balance = await get_balance(session, message.from_user.id)
        due = balance.due_usd_cents if currency == "USD" else balance.due_rub_kopeks
        suggested = suggested_payment_minor(due, currency)
        label = suggested_payment_button(suggested, currency) if suggested > 0 else None
        if message.text == label:
            amount = suggested
        elif message.text is None:
            await show_amount_choice(
                bot, message.from_user.id, currency, "Отправьте сумму числом.\n"
            )
            return
        elif message.text.startswith(PAY_SUGGESTED_PREFIX):
            await show_amount_choice(
                bot,
                message.from_user.id,
                currency,
                "Сумма долга изменилась. Выберите новую кнопку.\n",
            )
            return
        else:
            try:
                amount = parse_minor(message.text, name="Сумма")
            except ValueError as error:
                await show_amount_choice(
                    bot, message.from_user.id, currency, f"{error}. Отправьте сумму числом.\n"
                )
                return
        await state.update_data(amount_minor=amount)
        await state.set_state(Paying.screenshot)
        await message.answer(
            f"После перевода {money_text(amount, currency)} отправьте скриншот оплаты "
            "как фото или изображение-файл.",
            reply_markup=cancel_keyboard(),
        )

    @router.message(Paying.screenshot, F.photo | F.document)
    async def pay_screenshot(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        file_id: str | None = None
        kind: str | None = None
        if message.photo:
            file_id = message.photo[-1].file_id
            kind = "photo"
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            kind = "document"
        if file_id is None or kind is None:
            await message.answer("Нужен скриншот как фото или файл изображения.")
            return
        data = await state.get_data()
        currency = str(data.get("currency") or "")
        amount = data.get("amount_minor")
        if currency not in {"USD", "RUB"} or not isinstance(amount, int) or amount <= 0:
            await state.clear()
            await message.answer(
                "Сумма потеряна. Начните оплату заново.",
                reply_markup=main_keyboard(is_admin=message.from_user.id in admin_ids),
            )
            return
        async with sessions() as session:
            async with session.begin():
                payment = await submit_payment(
                    session,
                    message.from_user.id,
                    currency,
                    amount,
                    screenshot_file_id=file_id,
                    screenshot_kind=kind,
                )
                payment_id = payment.id
        await state.clear()
        await message.answer(
            f"Подтверждение #{payment_id} отправлено на проверку. "
            "Баланс изменится после подтверждения администратором.",
            reply_markup=main_keyboard(is_admin=message.from_user.id in admin_ids),
        )
        await send_admin_review(bot, sessions, payment_id, admin_ids)

    @router.message(Paying.screenshot)
    async def require_screenshot(message: Message) -> None:
        await message.answer(
            "Отправьте скриншот как фото или файл изображения либо нажмите «Отмена».",
            reply_markup=cancel_keyboard(),
        )

    return router
