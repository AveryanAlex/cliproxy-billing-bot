from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode

from cliproxy_billing.db import initialize_database, make_engine, make_sessions
from cliproxy_billing.ledger import upsert_user
from cliproxy_billing.models import BillingRun, KeyCharge, KeyOwnership
from cliproxy_billing.reminders import (
    current_debtors,
    make_reminder_scheduler,
    remind_debtors,
    report_debtors,
)


async def test_weekly_reminders_and_sorted_admin_report(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/reminders.db")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    async with sessions() as session:
        async with session.begin():
            for user_id, name in ((101, "Smaller"), (102, "<Larger & Co>"), (103, "Settled")):
                await upsert_user(session, user_id, name)
            run = BillingRun(
                start_date=date(2026, 9, 1),
                end_exclusive=date(2026, 9, 26),
                subscription_usd_cents=2000,
                fee_percent="0",
                rub_per_usd="80",
                total_usd_cents=2000,
                total_rub_kopeks=160000,
                status="published",
                created_by=999,
            )
            session.add(run)
            await session.flush()
            for user_id, due_cents in ((101, 500), (102, 1500)):
                key_id = f"key-{user_id}"
                session.add(KeyOwnership(keeper_key_id=key_id, user_id=user_id, label=key_id))
                session.add(
                    KeyCharge(
                        run_id=run.id,
                        keeper_key_id=key_id,
                        key_label=key_id,
                        usage_cost_usd="1",
                        requests=1,
                        principal_usd_cents=due_cents,
                        fee_usd_cents=0,
                        due_usd_cents=due_cents,
                        due_rub_kopeks=due_cents * 80,
                    )
                )

    debtors = await current_debtors(sessions)
    assert [person.user_id for person in debtors] == [102, 101]

    bot = Bot("123456:TEST")
    with patch.object(bot, "send_message", new_callable=AsyncMock) as send_message:
        await remind_debtors(bot, sessions)
        assert [call.args[0] for call in send_message.await_args_list] == [102, 101]
        assert "$15.00" in send_message.await_args_list[0].args[1]

    with (
        patch.object(bot, "get_chat", new_callable=AsyncMock) as get_chat,
        patch.object(bot, "send_message", new_callable=AsyncMock) as send_message,
    ):
        get_chat.side_effect = [
            SimpleNamespace(username="larger_user"),
            SimpleNamespace(username=None),
        ]
        await report_debtors(bot, sessions, frozenset({999}), ZoneInfo("Europe/Moscow"))
        assert send_message.await_count == 1
        assert send_message.await_args is not None
        report = send_message.await_args.args[1]
        assert report.index("$15.00") < report.index("$5.00")
        assert "&lt;Larger &amp; Co&gt;" in report
        assert '<a href="https://t.me/larger_user">@larger_user</a>' in report
        assert send_message.await_args.kwargs["parse_mode"] == ParseMode.HTML

    await bot.session.close()
    await engine.dispose()


async def test_reminder_cron_uses_moscow_time(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/schedule.db")
    sessions = make_sessions(engine)
    bot = Bot("123456:TEST")
    time_zone = ZoneInfo("Europe/Moscow")
    scheduler = make_reminder_scheduler(bot, sessions, frozenset({999}), time_zone)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    start = datetime(2026, 9, 26, 12, 0, tzinfo=time_zone)
    monday = jobs["monday-debt-reminders"].trigger.get_next_fire_time(None, start)
    thursday = jobs["thursday-debt-report"].trigger.get_next_fire_time(None, start)
    assert monday == datetime(2026, 9, 28, 14, 0, tzinfo=time_zone)
    assert thursday == datetime(2026, 10, 1, 14, 0, tzinfo=time_zone)
    await bot.session.close()
    await engine.dispose()
