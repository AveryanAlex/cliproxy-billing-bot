from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .admin_bot import admin_menu
from .ledger import upsert_user
from .screens import show_balance, show_history, show_keys, show_payment_start
from .ui import (
    ADD_KEY_BUTTON,
    ADMIN_BUTTON,
    BALANCE_BUTTON,
    HISTORY_BUTTON,
    KEYS_BUTTON,
    PAY_BUTTON,
)
from .user_bot import Linking


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
    }

    @router.message(F.chat.type == "private", F.text.in_(menu_labels))
    async def navigate(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.text is None:
            return
        user_id = message.from_user.id
        await state.clear()
        if message.text == ADMIN_BUTTON:
            if user_id not in admin_ids:
                await bot.send_message(user_id, "Раздел доступен только администратору.")
                return
            await bot.send_message(
                user_id, "Управление расчётами и платежами.", reply_markup=admin_menu()
            )
            return
        async with sessions() as session:
            async with session.begin():
                await upsert_user(session, user_id, message.from_user.full_name)
        if message.text == BALANCE_BUTTON:
            await show_balance(bot, sessions, user_id)
        elif message.text == HISTORY_BUTTON:
            await show_history(bot, sessions, user_id, time_zone)
        elif message.text == KEYS_BUTTON:
            await show_keys(bot, sessions, user_id)
        elif message.text == ADD_KEY_BUTTON:
            await state.set_state(Linking.key)
            await bot.send_message(user_id, "Отправьте API-ключ одним сообщением.")
        elif message.text == PAY_BUTTON:
            await show_payment_start(bot, sessions, user_id)

    return router
