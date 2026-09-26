from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .billing import BillingError, BillingService
from .keeper import KeeperError
from .ledger import LedgerError, get_balance, review_payment, submit_payment
from .models import BillingRun, KeyCharge, KeyOwnership, Payment, User
from .money import money_text, parse_minor, parse_positive_decimal
from .ui import balance_text, buttons, history_text, send_text
from .user_bot import send_admin_review

logger = logging.getLogger(__name__)


class NewBilling(StatesGroup):
    initial_date = State()
    subscription = State()
    fee = State()
    exchange_rate = State()


class ManualPayment(StatesGroup):
    user_id = State()
    currency = State()
    amount = State()


def admin_menu() -> InlineKeyboardMarkup:
    return buttons(
        (("🧮 Новый расчёт", "admin:new"), ("📋 Черновик", "admin:draft")),
        (("💳 Проверить оплаты", "admin:pending"), ("👥 Участники", "admin:users")),
        (("🔑 Непривязанные начисления", "admin:unlinked"),),
        (("➕ Занести платёж / аванс", "admin:manual"),),
    )


async def _run_user_amount(session: AsyncSession, run_id: int, user_id: int) -> tuple[int, int]:
    result = await session.scalars(
        select(KeyCharge)
        .join(KeyOwnership, KeyOwnership.keeper_key_id == KeyCharge.keeper_key_id)
        .where(KeyCharge.run_id == run_id, KeyOwnership.user_id == user_id)
    )
    charges = result.all()
    return (
        sum(charge.due_usd_cents for charge in charges),
        sum(charge.due_rub_kopeks for charge in charges),
    )


def make_admin_router(
    sessions: async_sessionmaker[AsyncSession],
    billing: BillingService,
    admin_ids: frozenset[int],
    time_zone: ZoneInfo,
) -> Router:
    router = Router(name="admin")
    router.message.filter(F.from_user.id.in_(admin_ids))
    router.callback_query.filter(F.from_user.id.in_(admin_ids))

    @router.message(Command("admin"))
    async def admin_home(message: Message, bot: Bot) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        await bot.send_message(
            message.from_user.id, "Управление расчётами и платежами.", reply_markup=admin_menu()
        )

    @router.callback_query(F.data == "admin:new")
    async def begin_billing(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        draft = await billing.existing_draft()
        if draft is not None:
            await state.clear()
            await send_text(
                bot,
                query.from_user.id,
                draft.text(),
                markup=buttons(
                    (("✅ Опубликовать", f"admin:publish:{draft.run_id}"),),
                    (("🗑 Отменить черновик", f"admin:discard:{draft.run_id}"),),
                ),
            )
            return
        await state.clear()
        if await billing.initial_date_needed():
            await state.set_state(NewBilling.initial_date)
            await bot.send_message(
                query.from_user.id,
                "Это первый расчёт. Введите дату начала периода YYYY-MM-DD. "
                "Всё до этой даты считается урегулированным вне бота.",
            )
        else:
            start = await billing.next_start_date()
            await state.set_state(NewBilling.subscription)
            await bot.send_message(
                query.from_user.id,
                f"Период: с {start} по {(billing.today() - timedelta(days=1))} включительно.\n"
                "Введите стоимость подписки в USD, без комиссии.",
            )

    @router.message(NewBilling.initial_date, F.text)
    async def billing_start_date(message: Message, state: FSMContext) -> None:
        try:
            start = date.fromisoformat((message.text or "").strip())
        except ValueError:
            await message.answer("Введите дату как YYYY-MM-DD.")
            return
        if start >= billing.today():
            await message.answer("Начало должно быть раньше сегодняшнего дня.")
            return
        await state.update_data(initial_start=start.isoformat())
        await state.set_state(NewBilling.subscription)
        await message.answer(
            f"Первый период: {start} — {billing.today() - timedelta(days=1)}.\n"
            "Введите стоимость подписки в USD, без комиссии."
        )

    @router.message(NewBilling.subscription, F.text)
    async def billing_subscription(message: Message, state: FSMContext) -> None:
        try:
            amount = parse_minor(message.text or "", name="Стоимость подписки")
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.update_data(subscription_usd_cents=amount)
        await state.set_state(NewBilling.fee)
        await message.answer("Введите комиссию в процентах, например 1 или 1.5. Можно 0.")

    @router.message(NewBilling.fee, F.text)
    async def billing_fee(message: Message, state: FSMContext) -> None:
        try:
            fee = parse_positive_decimal(message.text or "", name="Комиссия", allow_zero=True)
            if fee > 100:
                raise ValueError("Комиссия должна быть не больше 100%")
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.update_data(fee_percent=str(fee))
        await state.set_state(NewBilling.exchange_rate)
        await message.answer("Введите курс ₽ за $1, например 88.75.")

    @router.message(NewBilling.exchange_rate, F.text)
    async def billing_exchange_rate(message: Message, state: FSMContext, bot: Bot) -> None:
        try:
            rate = parse_positive_decimal(message.text or "", name="Курс")
            if rate < 1:
                raise ValueError("Курс должен быть не меньше 1 ₽ за доллар")
        except ValueError as error:
            await message.answer(str(error))
            return
        data = await state.get_data()
        await state.clear()
        try:
            draft = await billing.create_draft(
                admin_id=message.from_user.id if message.from_user else 0,
                initial_start=(
                    date.fromisoformat(str(data["initial_start"]))
                    if "initial_start" in data
                    else None
                ),
                subscription_usd_cents=int(data["subscription_usd_cents"]),
                fee_percent=Decimal(str(data["fee_percent"])),
                rub_per_usd=rate,
            )
        except (BillingError, KeeperError, ValueError) as error:
            await message.answer(
                f"Расчёт не выпущен: {error}\nИсправьте причину и начните заново через /admin."
            )
            return
        await send_text(
            bot,
            message.chat.id,
            draft.text(),
            markup=buttons(
                (("✅ Опубликовать", f"admin:publish:{draft.run_id}"),),
                (("🗑 Отменить черновик", f"admin:discard:{draft.run_id}"),),
            ),
        )

    @router.callback_query(F.data == "admin:draft")
    async def show_draft(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        draft = await billing.existing_draft()
        if draft is None:
            await bot.send_message(query.from_user.id, "Черновика нет.", reply_markup=admin_menu())
            return
        await send_text(
            bot,
            query.from_user.id,
            draft.text(),
            markup=buttons(
                (("✅ Опубликовать", f"admin:publish:{draft.run_id}"),),
                (("🗑 Отменить черновик", f"admin:discard:{draft.run_id}"),),
            ),
        )

    @router.callback_query(F.data.startswith("admin:discard:"))
    async def discard(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        try:
            await billing.discard(int((query.data or "").rsplit(":", 1)[-1]))
        except (BillingError, ValueError) as error:
            await bot.send_message(query.from_user.id, str(error))
            return
        await bot.send_message(query.from_user.id, "Черновик удалён.", reply_markup=admin_menu())

    @router.callback_query(F.data.startswith("admin:publish:"))
    async def publish(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        try:
            published = await billing.publish(int((query.data or "").rsplit(":", 1)[-1]))
        except (BillingError, ValueError) as error:
            await bot.send_message(query.from_user.id, str(error))
            return
        async with sessions() as session:
            run = await session.get(BillingRun, published.run_id)
        if run is None:
            return
        end_day = run.end_exclusive - timedelta(days=1)
        failed_notifications: list[int] = []
        for user_id in published.affected_users:
            async with sessions() as session:
                balance = await get_balance(session, user_id)
                amount_usd, amount_rub = await _run_user_amount(session, run.id, user_id)
            try:
                await bot.send_message(
                    user_id,
                    f"Новое начисление за {run.start_date} — {end_day}: "
                    f"{money_text(amount_usd, 'USD')} / {money_text(amount_rub, 'RUB')}.\n"
                    f"{balance_text(balance)}",
                    reply_markup=buttons(
                        (("📷 Оплатить", "pay:start"), ("📜 История", "user:history"))
                    ),
                )
            except TelegramAPIError:
                failed_notifications.append(user_id)
                logger.warning("Could not notify user %s about run %s", user_id, run.id)
        note = (
            f"Расчёт #{run.id} опубликован. Уведомлено: "
            f"{len(published.affected_users) - len(failed_notifications)}.\n"
            f"Непривязанные ключи: {money_text(published.unlinked_usd_cents, 'USD')}."
        )
        if failed_notifications:
            note += (
                f"\nНе удалось уведомить Telegram ID: {', '.join(map(str, failed_notifications))}."
            )
        await bot.send_message(query.from_user.id, note, reply_markup=admin_menu())

    @router.callback_query(F.data == "admin:pending")
    async def pending(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        async with sessions() as session:
            result = await session.scalars(
                select(Payment.id).where(Payment.status == "pending").order_by(Payment.id)
            )
            ids = list(result.all())
        if not ids:
            await bot.send_message(query.from_user.id, "Платежей на проверке нет.")
            return
        await bot.send_message(query.from_user.id, f"На проверке: {len(ids)}.")
        for payment_id in ids:
            await send_admin_review(bot, sessions, payment_id, frozenset({query.from_user.id}))

    @router.callback_query(F.data.startswith("review:"))
    async def review(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[1] not in {"yes", "no"}:
            return
        try:
            async with sessions() as session:
                async with session.begin():
                    payment = await review_payment(
                        session, int(parts[2]), query.from_user.id, approve=parts[1] == "yes"
                    )
                    user_id = payment.user_id
                balance = await get_balance(session, user_id)
        except (LedgerError, ValueError) as error:
            await bot.send_message(query.from_user.id, str(error))
            return
        status = "подтверждён" if parts[1] == "yes" else "отклонён"
        await bot.send_message(query.from_user.id, f"Платёж #{payment.id} {status}.")
        try:
            await bot.send_message(
                user_id,
                f"Ваш платёж #{payment.id} {status}.\n"
                + (
                    balance_text(balance)
                    if parts[1] == "yes"
                    else "Проверьте перевод и отправьте скриншот ещё раз."
                ),
                reply_markup=buttons((("📷 Оплатить", "pay:start"), ("💰 Баланс", "user:balance"))),
            )
        except TelegramAPIError:
            logger.warning("Could not notify user %s about payment %s", user_id, payment.id)

    @router.callback_query(F.data == "admin:users")
    async def users(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        async with sessions() as session:
            rows = list((await session.scalars(select(User).order_by(User.telegram_id))).all())
            lines = ["Участники:"]
            for user in rows:
                balance = await get_balance(session, user.telegram_id)
                lines.append(
                    f"• {user.display_name} · ID {user.telegram_id}: "
                    f"{money_text(balance.due_usd_cents, 'USD')} / "
                    f"{money_text(balance.due_rub_kopeks, 'RUB')}; аванс "
                    f"{money_text(balance.credit_usd_cents, 'USD')} и "
                    f"{money_text(balance.credit_rub_kopeks, 'RUB')}"
                )
            if not rows:
                lines.append("Пока никто не зарегистрировался.")
            else:
                lines.append("\nИстория человека: /person TELEGRAM_ID")
        await send_text(bot, query.from_user.id, "\n".join(lines), markup=admin_menu())

    @router.message(Command("person"))
    async def person_history(message: Message, bot: Bot) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Использование: /person TELEGRAM_ID")
            return
        user_id = int(parts[1])
        async with sessions() as session:
            user = await session.get(User, user_id)
            if user is None:
                await message.answer("Участник не найден.")
                return
            balance = await get_balance(session, user_id)
        await send_text(
            bot,
            message.from_user.id,
            f"{user.display_name} · Telegram ID {user_id}\n"
            f"{balance_text(balance)}\n\n{history_text(balance, time_zone)}",
            markup=admin_menu(),
        )

    @router.callback_query(F.data == "admin:unlinked")
    async def unlinked_charges(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        async with sessions() as session:
            result = await session.execute(
                select(KeyCharge, BillingRun)
                .join(BillingRun, BillingRun.id == KeyCharge.run_id)
                .outerjoin(KeyOwnership, KeyOwnership.keeper_key_id == KeyCharge.keeper_key_id)
                .where(
                    BillingRun.status == "published",
                    KeyOwnership.keeper_key_id.is_(None),
                    KeyCharge.due_usd_cents > 0,
                )
                .order_by(BillingRun.end_exclusive, KeyCharge.keeper_key_id)
            )
            rows = result.all()
        lines = ["Непривязанные начисления:"]
        for charge, run in rows:
            lines.append(
                f"• {charge.key_label} · ID {charge.keeper_key_id} · "
                f"{run.start_date} — {run.end_exclusive - timedelta(days=1)}: "
                f"{money_text(charge.due_usd_cents, 'USD')} / "
                f"{money_text(charge.due_rub_kopeks, 'RUB')}"
            )
        if not rows:
            lines.append("Нет.")
        await send_text(bot, query.from_user.id, "\n".join(lines), markup=admin_menu())

    @router.callback_query(F.data == "admin:manual")
    async def begin_manual(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        await state.clear()
        await state.set_state(ManualPayment.user_id)
        await bot.send_message(
            query.from_user.id,
            "Введите Telegram ID участника из раздела «Участники». "
            "Так можно занести уже полученный платёж или аванс.",
        )

    @router.message(ManualPayment.user_id, F.text)
    async def manual_user(message: Message, state: FSMContext) -> None:
        try:
            user_id = int((message.text or "").strip())
        except ValueError:
            await message.answer("Введите числовой Telegram ID.")
            return
        async with sessions() as session:
            user = await session.get(User, user_id)
        if user is None:
            await message.answer("Такой участник ещё не открыл бота через /start.")
            return
        await state.update_data(user_id=user_id)
        await state.set_state(ManualPayment.currency)
        await message.answer(
            f"Участник: {user.display_name}. Выберите валюту платежа.",
            reply_markup=buttons(
                (("💵 USD", "manual:currency:USD"), ("₽ RUB", "manual:currency:RUB")),
            ),
        )

    @router.callback_query(F.data.startswith("manual:currency:"), ManualPayment.currency)
    async def manual_currency(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        await query.answer()
        currency = (query.data or "").rsplit(":", 1)[-1]
        if currency not in {"USD", "RUB"}:
            return
        await state.update_data(currency=currency)
        await state.set_state(ManualPayment.amount)
        await bot.send_message(query.from_user.id, f"Введите полученную сумму в {currency}.")

    @router.message(ManualPayment.amount, F.text)
    async def manual_amount(message: Message, state: FSMContext, bot: Bot) -> None:
        try:
            amount = parse_minor(message.text or "", name="Сумма")
        except ValueError as error:
            await message.answer(str(error))
            return
        data = await state.get_data()
        user_id = int(data["user_id"])
        currency = str(data["currency"])
        await state.clear()
        async with sessions() as session:
            async with session.begin():
                payment = await submit_payment(
                    session,
                    user_id,
                    currency,
                    amount,
                    screenshot_file_id=None,
                    screenshot_kind=None,
                    manual=True,
                    reviewed_by=message.from_user.id if message.from_user else None,
                )
            balance = await get_balance(session, user_id)
        await bot.send_message(
            message.chat.id,
            f"Платёж #{payment.id} записан.\n{balance_text(balance)}",
            reply_markup=admin_menu(),
        )
        try:
            await bot.send_message(
                user_id,
                f"Администратор записал платёж {money_text(amount, currency)}.\n"
                f"{balance_text(balance)}",
            )
        except TelegramAPIError:
            logger.warning("Could not notify user %s about manual payment %s", user_id, payment.id)

    return router
