from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cliproxy_billing.db import initialize_database, make_engine, make_sessions
from cliproxy_billing.ledger import (
    get_balance,
    link_key,
    reallocate_user,
    review_payment,
    submit_payment,
    upsert_user,
)
from cliproxy_billing.models import BillingRun, KeyCharge


async def add_charge(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    due_usd: int,
    due_rub: int,
) -> None:
    run = BillingRun(
        start_date=start,
        end_exclusive=end,
        subscription_usd_cents=due_usd,
        fee_percent="0",
        rub_per_usd="80",
        total_usd_cents=due_usd,
        total_rub_kopeks=due_rub,
        status="published",
        created_by=999,
    )
    session.add(run)
    await session.flush()
    session.add(
        KeyCharge(
            run_id=run.id,
            keeper_key_id="key",
            key_label="Key",
            usage_cost_usd=str(Decimal(due_usd) / 100),
            requests=1,
            principal_usd_cents=due_usd,
            fee_usd_cents=0,
            due_usd_cents=due_usd,
            due_rub_kopeks=due_rub,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_mixed_payments_fifo_and_ruble_advance() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    try:
        async with sessions() as session:
            async with session.begin():
                await upsert_user(session, 101, "Person")
                await link_key(session, 101, "key", "Key")
                await add_charge(
                    session,
                    start=date(2026, 7, 1),
                    end=date(2026, 8, 1),
                    due_usd=1010,
                    due_rub=80800,
                )
                await add_charge(
                    session,
                    start=date(2026, 8, 1),
                    end=date(2026, 9, 1),
                    due_usd=1010,
                    due_rub=90900,
                )
                await submit_payment(
                    session,
                    101,
                    "RUB",
                    40000,
                    screenshot_file_id=None,
                    screenshot_kind=None,
                    manual=True,
                    reviewed_by=999,
                )
                await submit_payment(
                    session,
                    101,
                    "USD",
                    800,
                    screenshot_file_id=None,
                    screenshot_kind=None,
                    manual=True,
                    reviewed_by=999,
                )
            balance = await get_balance(session, 101)
            assert balance.due_usd_cents == 720
            assert balance.due_rub_kopeks == 64800
            assert balance.charges[0].remaining_usd_cents == 0
            assert balance.charges[1].remaining_usd_cents == 720

            await session.rollback()
            async with session.begin():
                await submit_payment(
                    session,
                    101,
                    "RUB",
                    100000,
                    screenshot_file_id=None,
                    screenshot_kind=None,
                    manual=True,
                    reviewed_by=999,
                )
            balance = await get_balance(session, 101)
            assert balance.due_usd_cents == 0
            assert balance.credit_rub_kopeks == 35200

            await session.rollback()
            async with session.begin():
                await add_charge(
                    session,
                    start=date(2026, 9, 1),
                    end=date(2026, 10, 1),
                    due_usd=400,
                    due_rub=40000,
                )
                await reallocate_user(session, 101)
            balance = await get_balance(session, 101)
            assert balance.due_usd_cents == 48
            assert balance.due_rub_kopeks == 4800
            assert balance.credit_rub_kopeks == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rejection_does_not_change_balance() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    try:
        async with sessions() as session:
            async with session.begin():
                await upsert_user(session, 101, "Person")
                await link_key(session, 101, "key", "Key")
                await add_charge(
                    session,
                    start=date(2026, 8, 1),
                    end=date(2026, 9, 1),
                    due_usd=100,
                    due_rub=8000,
                )
                payment = await submit_payment(
                    session,
                    101,
                    "USD",
                    100,
                    screenshot_file_id="file-id",
                    screenshot_kind="photo",
                )
            assert (await get_balance(session, 101)).due_usd_cents == 100
            payment_id = payment.id
            await session.rollback()
            async with session.begin():
                await review_payment(session, payment_id, 999, approve=False)
            assert (await get_balance(session, 101)).due_usd_cents == 100
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_key_cannot_be_claimed_by_second_user() -> None:
    from cliproxy_billing.ledger import LedgerError

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    try:
        async with sessions() as session:
            async with session.begin():
                await upsert_user(session, 101, "First")
                await upsert_user(session, 102, "Second")
                assert await link_key(session, 101, "key", "Key")
                assert not await link_key(session, 101, "key", "Key")
                with pytest.raises(LedgerError, match="другому аккаунту"):
                    await link_key(session, 102, "key", "Key")
    finally:
        await engine.dispose()
