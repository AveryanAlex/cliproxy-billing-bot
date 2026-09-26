from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .ledger import get_balance, user_keys
from .ui import balance_text, buttons, history_text, send_text, user_menu


async def show_balance(bot: Bot, sessions: async_sessionmaker[AsyncSession], user_id: int) -> None:
    async with sessions() as session:
        balance = await get_balance(session, user_id)
    await send_text(bot, user_id, balance_text(balance), markup=user_menu())


async def show_history(
    bot: Bot, sessions: async_sessionmaker[AsyncSession], user_id: int, time_zone: ZoneInfo
) -> None:
    async with sessions() as session:
        balance = await get_balance(session, user_id)
    await send_text(bot, user_id, history_text(balance, time_zone), markup=user_menu())


async def show_keys(bot: Bot, sessions: async_sessionmaker[AsyncSession], user_id: int) -> None:
    async with sessions() as session:
        keys = await user_keys(session, user_id)
    labels = "\n".join(f"• {key.label} (ID {key.keeper_key_id})" for key in keys)
    await bot.send_message(
        user_id,
        "Ваши ключи:\n" + (labels or "Пока нет привязанных ключей."),
        reply_markup=buttons((("➕ Добавить ключ", "user:add_key"),)),
    )


async def show_payment_start(
    bot: Bot, sessions: async_sessionmaker[AsyncSession], user_id: int
) -> None:
    async with sessions() as session:
        balance = await get_balance(session, user_id)
    await bot.send_message(
        user_id,
        balance_text(balance) + "\n\nВыберите валюту перевода:",
        reply_markup=buttons((("💵 USD", "pay:choose:USD"), ("₽ RUB", "pay:choose:RUB"))),
    )
