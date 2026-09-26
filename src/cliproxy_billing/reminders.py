from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .ledger import get_balance
from .models import User
from .money import money_text
from .ui import send_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Debtor:
    user_id: int
    display_name: str
    due_usd_cents: int
    due_rub_kopeks: int


async def current_debtors(sessions: async_sessionmaker[AsyncSession]) -> list[Debtor]:
    async with sessions() as session:
        users = list((await session.scalars(select(User).order_by(User.telegram_id))).all())
        debtors: list[Debtor] = []
        for user in users:
            balance = await get_balance(session, user.telegram_id)
            if balance.due_usd_cents > 0 or balance.due_rub_kopeks > 0:
                debtors.append(
                    Debtor(
                        user_id=user.telegram_id,
                        display_name=user.display_name,
                        due_usd_cents=balance.due_usd_cents,
                        due_rub_kopeks=balance.due_rub_kopeks,
                    )
                )
    return sorted(
        debtors,
        key=lambda person: (-person.due_usd_cents, -person.due_rub_kopeks, person.user_id),
    )


async def remind_debtors(bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
    debtors = await current_debtors(sessions)
    sent = 0
    for person in debtors:
        try:
            await bot.send_message(
                person.user_id,
                "Напоминание об оплате подписки.\n"
                f"Текущий долг: {money_text(person.due_usd_cents, 'USD')} "
                f"или {money_text(person.due_rub_kopeks, 'RUB')}.\n"
                "После перевода отправьте скриншот через «Оплатить». "
                "Если скриншот уже на проверке, дождитесь ответа администратора.",
            )
            sent += 1
        except TelegramAPIError:
            logger.warning("Could not send debt reminder to Telegram ID %s", person.user_id)
    logger.info("Weekly debt reminders sent: %s of %s", sent, len(debtors))


async def report_debtors(
    bot: Bot,
    sessions: async_sessionmaker[AsyncSession],
    admin_ids: frozenset[int],
    time_zone: ZoneInfo,
) -> None:
    debtors = await current_debtors(sessions)
    lines = [f"Должники на {datetime.now(time_zone):%d.%m.%Y %H:%M} ({time_zone.key}):"]
    for index, person in enumerate(debtors, 1):
        name = escape(person.display_name)
        try:
            chat = await bot.get_chat(person.user_id)
            username = chat.username
        except TelegramAPIError:
            logger.warning("Could not get username for Telegram ID %s", person.user_id)
            username = None
        contact = (
            f' · <a href="https://t.me/{quote(username, safe="")}">@{escape(username)}</a>'
            if username
            else ""
        )
        lines.append(
            f"{index}. {name}{contact} · ID {person.user_id}: "
            f"{money_text(person.due_usd_cents, 'USD')} / "
            f"{money_text(person.due_rub_kopeks, 'RUB')}"
        )
    if not debtors:
        lines.append("Должников нет.")
    report = "\n".join(lines)
    for admin_id in sorted(admin_ids):
        try:
            await send_text(bot, admin_id, report, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            logger.warning("Could not send debt report to admin %s", admin_id)
    logger.info("Weekly debt report prepared for %s debtors", len(debtors))


def make_reminder_scheduler(
    bot: Bot,
    sessions: async_sessionmaker[AsyncSession],
    admin_ids: frozenset[int],
    time_zone: ZoneInfo,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=time_zone)
    job_options = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    scheduler.add_job(
        remind_debtors,
        CronTrigger(day_of_week="mon", hour=14, minute=0, timezone=time_zone),
        args=(bot, sessions),
        id="monday-debt-reminders",
        **job_options,
    )
    scheduler.add_job(
        report_debtors,
        CronTrigger(day_of_week="thu", hour=14, minute=0, timezone=time_zone),
        args=(bot, sessions, admin_ids, time_zone),
        id="thursday-debt-report",
        **job_options,
    )
    return scheduler
