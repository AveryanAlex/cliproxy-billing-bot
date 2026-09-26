from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .keeper import KeeperClient, KeeperError
from .ledger import LedgerError, get_balance, link_key, submit_payment, upsert_user, user_keys
from .models import Payment, User
from .money import money_text, parse_minor
from .ui import balance_text, buttons, history_text, send_text, user_menu

logger = logging.getLogger(__name__)


class Linking(StatesGroup):
    key = State()


class Paying(StatesGroup):
    amount = State()
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

    async def show_balance(bot: Bot, user_id: int) -> None:
        async with sessions() as session:
            balance = await get_balance(session, user_id)
        await send_text(bot, user_id, balance_text(balance), markup=user_menu())

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        await state.clear()
        user_id = message.from_user.id
        async with sessions() as session:
            async with session.begin():
                await upsert_user(session, user_id, message.from_user.full_name)
            keys = await user_keys(session, user_id)
        if not keys:
            await state.set_state(Linking.key)
            await bot.send_message(
                user_id,
                "Привет! Отправьте ваш API-ключ одним сообщением. "
                "После привязки можно добавить ещё один.",
            )
        else:
            await show_balance(bot, user_id)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        await state.clear()
        await bot.send_message(message.from_user.id, "Действие отменено.", reply_markup=user_menu())

    @router.callback_query(F.data == "user:add_key")
    async def add_key(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        await state.set_state(Linking.key)
        await bot.send_message(query.from_user.id, "Отправьте ещё один API-ключ одним сообщением.")

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
            reply_markup=buttons(
                (("➕ Добавить ещё ключ", "user:add_key"), ("✅ Готово", "user:balance")),
            ),
        )

    @router.message(Linking.key)
    async def require_key_text(message: Message) -> None:
        if message.chat.type == "private":
            await message.answer("Отправьте API-ключ текстом одним сообщением.")

    @router.callback_query(F.data == "user:balance")
    async def balance_callback(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        await show_balance(bot, query.from_user.id)

    @router.callback_query(F.data == "user:history")
    async def history_callback(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        async with sessions() as session:
            balance = await get_balance(session, query.from_user.id)
        await send_text(
            bot, query.from_user.id, history_text(balance, time_zone), markup=user_menu()
        )

    @router.callback_query(F.data == "user:keys")
    async def keys_callback(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        async with sessions() as session:
            keys = await user_keys(session, query.from_user.id)
        labels = "\n".join(f"• {key.label} (ID {key.keeper_key_id})" for key in keys)
        await bot.send_message(
            query.from_user.id,
            "Ваши ключи:\n" + (labels or "Пока нет привязанных ключей."),
            reply_markup=buttons((("➕ Добавить ключ", "user:add_key"),)),
        )

    @router.callback_query(F.data == "pay:start")
    async def pay_start(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        async with sessions() as session:
            balance = await get_balance(session, query.from_user.id)
        await bot.send_message(
            query.from_user.id,
            balance_text(balance) + "\n\nВыберите валюту перевода:",
            reply_markup=buttons(
                (("💵 USD", "pay:choose:USD"), ("₽ RUB", "pay:choose:RUB")),
            ),
        )

    @router.callback_query(F.data.startswith("pay:choose:"))
    async def pay_choose(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        currency = (query.data or "").rsplit(":", 1)[-1]
        if currency not in {"USD", "RUB"}:
            return
        async with sessions() as session:
            balance = await get_balance(session, query.from_user.id)
        due = balance.due_usd_cents if currency == "USD" else balance.due_rub_kopeks
        rows: list[tuple[tuple[str, str], ...]] = []
        if due > 0:
            rows.append(((f"Погасить всё: {money_text(due, currency)}", f"pay:full:{currency}"),))
        rows.append((("Другая сумма / аванс", f"pay:custom:{currency}"),))
        await bot.send_message(
            query.from_user.id,
            f"Валюта: {currency}. Укажите сумму перевода или погасите текущий долг.",
            reply_markup=buttons(*rows),
        )

    @router.callback_query(F.data.startswith("pay:full:"))
    async def pay_full(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        currency = (query.data or "").rsplit(":", 1)[-1]
        if currency not in {"USD", "RUB"}:
            return
        async with sessions() as session:
            balance = await get_balance(session, query.from_user.id)
        amount = balance.due_usd_cents if currency == "USD" else balance.due_rub_kopeks
        if amount <= 0:
            await bot.send_message(
                query.from_user.id, "Долга нет. Для аванса выберите другую сумму."
            )
            return
        await state.update_data(currency=currency, amount_minor=amount)
        await state.set_state(Paying.screenshot)
        await bot.send_message(
            query.from_user.id,
            f"После перевода {money_text(amount, currency)} отправьте скриншот оплаты "
            "как фото или изображение-файл.",
        )

    @router.callback_query(F.data.startswith("pay:custom:"))
    async def pay_custom(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        currency = (query.data or "").rsplit(":", 1)[-1]
        if currency not in {"USD", "RUB"}:
            return
        await state.update_data(currency=currency)
        await state.set_state(Paying.amount)
        await bot.send_message(
            query.from_user.id,
            f"Введите фактически отправленную сумму в {currency}, например 12.34.",
        )

    @router.message(Paying.amount, F.text)
    async def pay_amount(message: Message, state: FSMContext) -> None:
        if message.text is None:
            return
        try:
            amount = parse_minor(message.text, name="Сумма")
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.update_data(amount_minor=amount)
        await state.set_state(Paying.screenshot)
        data = await state.get_data()
        currency = str(data.get("currency"))
        await message.answer(
            f"После перевода {money_text(amount, currency)} отправьте скриншот оплаты "
            "как фото или изображение-файл."
        )

    @router.message(Paying.amount)
    async def require_amount(message: Message) -> None:
        await message.answer("Введите сумму числом, например 12.34.")

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
            await message.answer("Сумма потеряна. Начните оплату заново.", reply_markup=user_menu())
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
            reply_markup=user_menu(),
        )
        await send_admin_review(bot, sessions, payment_id, admin_ids)

    @router.message(Paying.screenshot)
    async def require_screenshot(message: Message) -> None:
        await message.answer("Отправьте скриншот как фото или файл изображения.")

    return router
