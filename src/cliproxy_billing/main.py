from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from .admin_bot import make_admin_router
from .billing import BillingService
from .config import Settings
from .db import initialize_database, make_engine, make_sessions
from .keeper import KeeperClient
from .user_bot import make_user_router


async def register_commands(bot: Bot, admin_ids: frozenset[int]) -> None:
    user_commands = [
        BotCommand(command="start", description="Открыть бота и баланс"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ]
    admin_commands = [
        user_commands[0],
        BotCommand(command="admin", description="Управление расчётами и платежами"),
        BotCommand(command="person", description="История участника по Telegram ID"),
        user_commands[1],
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    for admin_id in sorted(admin_ids):
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramBadRequest:
            logging.warning("Could not register commands for admin chat %s", admin_id)


async def main() -> None:
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    await initialize_database(engine)
    sessions = make_sessions(engine)
    keeper = KeeperClient(settings.keeper_base_url, settings.keeper_login_password)
    billing = BillingService(sessions, keeper, settings.time_zone)
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(
        make_admin_router(sessions, billing, settings.admin_telegram_ids, settings.time_zone)
    )
    dispatcher.include_router(
        make_user_router(sessions, keeper, settings.admin_telegram_ids, settings.time_zone)
    )
    try:
        await register_commands(bot, settings.admin_telegram_ids)
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        await keeper.aclose()
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
