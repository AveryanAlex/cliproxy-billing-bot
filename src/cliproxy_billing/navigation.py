from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .ledger import upsert_user, user_keys
from .screens import show_balance, show_history, show_keys, show_payment_start
from .ui import (
    ADD_KEY_BUTTON,
    ADMIN_BUTTON,
    BACK_BUTTON,
    BALANCE_BUTTON,
    CANCEL_BUTTON,
    HISTORY_BUTTON,
    KEYS_BUTTON,
    PAY_BUTTON,
    admin_keyboard,
    cancel_keyboard,
    main_keyboard,
)
from .user_bot import Linking, Paying


def make_navigation_router(
    sessions: async_sessionmaker[AsyncSession],
    admin_ids: frozenset[int],
    time_zone: ZoneInfo,
) -> Router:
    router = Router(name="navigation")
    menu_labels = {
        BALANCE_BUTTON,
        HISTORY_BUTTON,
        KEYS_BUTTON,
        ADD_KEY_BUTTON,
        PAY_BUTTON,
        ADMIN_BUTTON,
        BACK_BUTTON,
    }

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
                "Привет! Отправьте API-ключ одним сообщением. "
                "После привязки можно добавить ещё один.",
                reply_markup=cancel_keyboard(),
            )
        else:
            await show_balance(bot, sessions, user_id, is_admin=user_id in admin_ids)

    @router.message(Command("cancel"))
    @router.message(F.chat.type == "private", F.text == CANCEL_BUTTON)
    async def cancel(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        previous = await state.get_state()
        await state.clear()
        is_admin = message.from_user.id in admin_ids
        in_admin_flow = (
            is_admin
            and previous is not None
            and previous.startswith(("NewBilling:", "ManualPayment:", "DraftReview:"))
        )
        await bot.send_message(
            message.from_user.id,
            "Действие отменено. Выберите раздел.",
            reply_markup=admin_keyboard() if in_admin_flow else main_keyboard(is_admin=is_admin),
        )

    @router.message(F.chat.type == "private", F.text.in_(menu_labels))
    async def navigate(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.text is None:
            return
        user_id = message.from_user.id
        await state.clear()
        if message.text == BACK_BUTTON:
            if user_id in admin_ids:
                await bot.send_message(
                    user_id, "Главное меню.", reply_markup=main_keyboard(is_admin=True)
                )
            else:
                await bot.send_message(
                    user_id, "Выберите действие.", reply_markup=main_keyboard(is_admin=False)
                )
            return
        if message.text == ADMIN_BUTTON:
            if user_id not in admin_ids:
                await bot.send_message(
                    user_id,
                    "Раздел доступен только администратору.",
                    reply_markup=main_keyboard(is_admin=False),
                )
                return
            await bot.send_message(
                user_id, "Управление расчётами и платежами.", reply_markup=admin_keyboard()
            )
            return
        async with sessions() as session:
            async with session.begin():
                await upsert_user(session, user_id, message.from_user.full_name)
        if message.text == BALANCE_BUTTON:
            await show_balance(bot, sessions, user_id, is_admin=user_id in admin_ids)
        elif message.text == HISTORY_BUTTON:
            await show_history(bot, sessions, user_id, time_zone, is_admin=user_id in admin_ids)
        elif message.text == KEYS_BUTTON:
            await show_keys(bot, sessions, user_id, is_admin=user_id in admin_ids)
        elif message.text == ADD_KEY_BUTTON:
            await state.set_state(Linking.key)
            await bot.send_message(
                user_id, "Отправьте API-ключ одним сообщением.", reply_markup=cancel_keyboard()
            )
        elif message.text == PAY_BUTTON:
            await state.set_state(Paying.currency)
            await show_payment_start(bot, sessions, user_id)

    return router


def make_fallback_router(admin_ids: frozenset[int]) -> Router:
    router = Router(name="fallback")

    @router.message(F.chat.type == "private")
    async def unknown_message(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.clear()
        if message.from_user is None:
            return
        await bot.send_message(
            message.from_user.id,
            "Не понял сообщение. Возможно, бот перезапустился. Выберите действие на клавиатуре.",
            reply_markup=main_keyboard(is_admin=message.from_user.id in admin_ids),
        )

    return router
