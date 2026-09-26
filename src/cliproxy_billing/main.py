from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .admin_bot import make_admin_router
from .billing import BillingService
from .config import Settings
from .db import initialize_database, make_engine, make_sessions
from .keeper import KeeperClient
from .user_bot import make_user_router


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
