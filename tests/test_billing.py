from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from cliproxy_billing.billing import BillingService
from cliproxy_billing.db import initialize_database, make_engine, make_sessions
from cliproxy_billing.keeper import KeeperClient, KeeperKey, KeyUsage, UsageAnalysis
from cliproxy_billing.ledger import get_balance, link_key, upsert_user
from cliproxy_billing.models import BillingRun, KeyCharge


class FakeKeeper:
    async def active_keys(self) -> tuple[KeeperKey, ...]:
        return (
            KeeperKey("1", "Alice"),
            KeeperKey("2", "Bob"),
            KeeperKey("3", "Unused"),
        )

    async def analysis(self, _start: date, _end: date) -> UsageAnalysis:
        return UsageAnalysis(
            keys=(
                KeyUsage("1", "Alice", Decimal("30"), 10),
                KeyUsage("2", "Bob", Decimal("10"), 5),
            ),
            total_cost_usd=Decimal("40"),
        )


@pytest.mark.asyncio
async def test_draft_and_late_key_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await initialize_database(engine)
    sessions = make_sessions(engine)
    service = BillingService(sessions, cast(KeeperClient, FakeKeeper()), ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr(service, "today", lambda: date(2026, 9, 26))
    try:
        async with sessions() as session:
            async with session.begin():
                await upsert_user(session, 101, "Person")
                await link_key(session, 101, "1", "Alice")

        draft = await service.create_draft(
            admin_id=999,
            initial_start=date(2026, 8, 26),
            subscription_usd_cents=40000,
            fee_percent=Decimal("1"),
            rub_per_usd=Decimal("80"),
        )
        assert draft.total_usd_cents == 40400
        assert draft.total_rub_kopeks == 3232000
        assert draft.unlinked_usd_cents == 10100
        assert sum(row.due_usd_cents for row in draft.charges) == 40400

        async with sessions() as session:
            count = len(list((await session.scalars(select(BillingRun))).all()))
            assert count == 1
            # Preview is not visible in the user's balance.
            assert (await get_balance(session, 101)).due_usd_cents == 0

        published = await service.publish(draft.run_id)
        assert published.affected_users == (101,)
        async with sessions() as session:
            assert (await get_balance(session, 101)).due_usd_cents == 30300
            charges = list((await session.scalars(select(KeyCharge))).all())
            assert {row.keeper_key_id: row.due_usd_cents for row in charges} == {
                "1": 30300,
                "2": 10100,
                "3": 0,
            }
            await session.rollback()
            async with session.begin():
                await link_key(session, 101, "2", "Bob")
            assert (await get_balance(session, 101)).due_usd_cents == 40400
    finally:
        await engine.dispose()
