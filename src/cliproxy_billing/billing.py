from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .keeper import KeeperClient, KeeperError, KeyUsage
from .ledger import reallocate_user
from .models import BillingRun, KeyCharge, KeyOwnership, User, utc_now
from .money import allocate_largest_remainder, from_minor, money_text, to_minor


class BillingError(ValueError):
    pass


@dataclass(frozen=True)
class ChargePreview:
    key_id: str
    label: str
    owner_name: str | None
    usage_cost_usd: Decimal
    due_usd_cents: int
    due_rub_kopeks: int


@dataclass(frozen=True)
class DraftPreview:
    run_id: int
    start_date: date
    end_exclusive: date
    subscription_usd_cents: int
    fee_percent: Decimal
    rub_per_usd: Decimal
    total_usd_cents: int
    total_rub_kopeks: int
    charges: tuple[ChargePreview, ...]

    @property
    def unlinked_usd_cents(self) -> int:
        return sum(row.due_usd_cents for row in self.charges if row.owner_name is None)

    def text(self) -> str:
        end_inclusive = date.fromordinal(self.end_exclusive.toordinal() - 1)
        rows = [
            f"Черновик #{self.run_id}: {self.start_date} — {end_inclusive}",
            f"Подписка: {money_text(self.subscription_usd_cents, 'USD')}",
            f"Комиссия: {self.fee_percent}% · курс: {self.rub_per_usd} ₽/$",
            (
                f"К оплате: {money_text(self.total_usd_cents, 'USD')} "
                f"или {money_text(self.total_rub_kopeks, 'RUB')}"
            ),
            "",
            "Начисления по ключам:",
        ]
        for row in self.charges:
            owner = row.owner_name or "не привязан"
            rows.append(
                f"• {row.label} ({owner}): {money_text(row.due_usd_cents, 'USD')} / "
                f"{money_text(row.due_rub_kopeks, 'RUB')}; использование "
                f"{row.usage_cost_usd} USD"
            )
        if self.unlinked_usd_cents:
            rows.extend(
                [
                    "",
                    f"Ожидают владельца: {money_text(self.unlinked_usd_cents, 'USD')}. "
                    "Они не перераспределяются между остальными.",
                ]
            )
        return "\n".join(rows)


@dataclass(frozen=True)
class PublishedRun:
    run_id: int
    affected_users: tuple[int, ...]
    unlinked_usd_cents: int


class BillingService:
    def __init__(
        self,
        sessions: async_sessionmaker,
        keeper: KeeperClient,
        time_zone: ZoneInfo,
    ) -> None:
        self._sessions = sessions
        self._keeper = keeper
        self._time_zone = time_zone
        self._lock = asyncio.Lock()

    def today(self) -> date:
        return datetime.now(self._time_zone).date()

    async def _latest_published(self, session: AsyncSession) -> BillingRun | None:
        result = await session.scalars(
            select(BillingRun)
            .where(BillingRun.status == "published")
            .order_by(BillingRun.end_exclusive.desc(), BillingRun.id.desc())
            .limit(1)
        )
        return result.first()

    async def initial_date_needed(self) -> bool:
        async with self._sessions() as session:
            return await self._latest_published(session) is None

    async def next_start_date(self) -> date | None:
        async with self._sessions() as session:
            previous = await self._latest_published(session)
            return previous.end_exclusive if previous else None

    async def existing_draft(self) -> DraftPreview | None:
        async with self._sessions() as session:
            result = await session.scalars(
                select(BillingRun)
                .where(BillingRun.status == "draft")
                .order_by(BillingRun.id.desc())
                .limit(1)
            )
            run = result.first()
            if run is None:
                return None
            return await self._preview(session, run)

    async def create_draft(
        self,
        *,
        admin_id: int,
        initial_start: date | None,
        subscription_usd_cents: int,
        fee_percent: Decimal,
        rub_per_usd: Decimal,
    ) -> DraftPreview:
        if subscription_usd_cents <= 0:
            raise BillingError("Сумма подписки должна быть больше нуля")
        if not fee_percent.is_finite() or fee_percent < 0 or fee_percent > 100:
            raise BillingError("Комиссия должна быть от 0 до 100%")
        if not rub_per_usd.is_finite() or rub_per_usd < 1:
            raise BillingError("Курс должен быть не меньше 1 ₽ за доллар")

        async with self._lock:
            async with self._sessions() as session:
                existing = await session.scalar(
                    select(BillingRun.id).where(BillingRun.status == "draft").limit(1)
                )
                if existing is not None:
                    raise BillingError("Уже есть черновик; подтвердите или отмените его")
                previous = await self._latest_published(session)
                if previous:
                    start_date = previous.end_exclusive
                    if initial_start is not None and initial_start != start_date:
                        raise BillingError("Следующий период начинается после прошлого расчёта")
                else:
                    if initial_start is None:
                        raise BillingError("Для первого расчёта укажите начальную дату")
                    start_date = initial_start

            end_exclusive = self.today()
            if start_date >= end_exclusive:
                raise BillingError("Нет завершённых дней для нового расчёта")

            try:
                active_keys = await self._keeper.active_keys()
                analysis = await self._keeper.analysis(start_date, end_exclusive)
            except KeeperError:
                raise

            by_id: dict[str, KeyUsage] = {key.key_id: key for key in analysis.keys}
            if len(by_id) != len(analysis.keys):
                raise BillingError("Keeper вернул повторяющиеся ID ключей")
            for key in active_keys:
                by_id.setdefault(
                    key.id,
                    KeyUsage(key_id=key.id, label=key.label, cost_usd=Decimal(0), requests=0),
                )
            weights = {key_id: row.cost_usd for key_id, row in by_id.items()}
            if not weights or sum(weights.values(), Decimal(0)) <= 0:
                raise BillingError(
                    "Общее платное использование равно нулю; проверьте данные Keeper"
                )
            principal = allocate_largest_remainder(subscription_usd_cents, weights)
            fee_total = to_minor(from_minor(subscription_usd_cents) * fee_percent / Decimal(100))
            fees = allocate_largest_remainder(
                fee_total, {key_id: Decimal(value) for key_id, value in principal.items()}
            )
            due = {key_id: principal[key_id] + fees[key_id] for key_id in principal}
            total_usd = subscription_usd_cents + fee_total
            total_rub = to_minor(from_minor(total_usd) * rub_per_usd)
            rubles = allocate_largest_remainder(
                total_rub, {key_id: Decimal(value) for key_id, value in due.items()}
            )

            async with self._sessions() as session:
                async with session.begin():
                    previous_again = await self._latest_published(session)
                    if (previous_again.end_exclusive if previous_again else None) != (
                        previous.end_exclusive if previous else None
                    ):
                        raise BillingError("Расчётный период изменился; создайте черновик заново")
                    existing_again = await session.scalar(
                        select(BillingRun.id).where(BillingRun.status == "draft").limit(1)
                    )
                    if existing_again is not None:
                        raise BillingError("Уже есть черновик")
                    run = BillingRun(
                        start_date=start_date,
                        end_exclusive=end_exclusive,
                        subscription_usd_cents=subscription_usd_cents,
                        fee_percent=str(fee_percent),
                        rub_per_usd=str(rub_per_usd),
                        total_usd_cents=total_usd,
                        total_rub_kopeks=total_rub,
                        status="draft",
                        created_by=admin_id,
                    )
                    session.add(run)
                    await session.flush()
                    for key_id in sorted(by_id):
                        row = by_id[key_id]
                        session.add(
                            KeyCharge(
                                run_id=run.id,
                                keeper_key_id=key_id,
                                key_label=row.label[:200],
                                usage_cost_usd=str(row.cost_usd),
                                requests=row.requests,
                                principal_usd_cents=principal[key_id],
                                fee_usd_cents=fees[key_id],
                                due_usd_cents=due[key_id],
                                due_rub_kopeks=rubles[key_id],
                            )
                        )
                    await session.flush()
                    preview = await self._preview(session, run)
            return preview

    async def _preview(self, session: AsyncSession, run: BillingRun) -> DraftPreview:
        result = await session.execute(
            select(KeyCharge, User)
            .outerjoin(KeyOwnership, KeyOwnership.keeper_key_id == KeyCharge.keeper_key_id)
            .outerjoin(User, User.telegram_id == KeyOwnership.user_id)
            .where(KeyCharge.run_id == run.id)
            .order_by(KeyCharge.due_usd_cents.desc(), KeyCharge.keeper_key_id)
        )
        charges = tuple(
            ChargePreview(
                key_id=charge.keeper_key_id,
                label=charge.key_label,
                owner_name=user.display_name if user is not None else None,
                usage_cost_usd=Decimal(charge.usage_cost_usd),
                due_usd_cents=charge.due_usd_cents,
                due_rub_kopeks=charge.due_rub_kopeks,
            )
            for charge, user in result.all()
        )
        return DraftPreview(
            run_id=run.id,
            start_date=run.start_date,
            end_exclusive=run.end_exclusive,
            subscription_usd_cents=run.subscription_usd_cents,
            fee_percent=Decimal(run.fee_percent),
            rub_per_usd=Decimal(run.rub_per_usd),
            total_usd_cents=run.total_usd_cents,
            total_rub_kopeks=run.total_rub_kopeks,
            charges=charges,
        )

    async def publish(self, run_id: int) -> PublishedRun:
        async with self._lock:
            async with self._sessions() as session:
                async with session.begin():
                    run = await session.get(BillingRun, run_id)
                    if run is None or run.status != "draft":
                        raise BillingError("Черновик не найден или уже опубликован")
                    if run.end_exclusive != self.today():
                        raise BillingError("Смена дня: отмените черновик и рассчитайте заново")
                    run.status = "published"
                    run.published_at = utc_now()
                    result = await session.execute(
                        select(KeyCharge, KeyOwnership)
                        .outerjoin(
                            KeyOwnership,
                            KeyOwnership.keeper_key_id == KeyCharge.keeper_key_id,
                        )
                        .where(KeyCharge.run_id == run.id)
                    )
                    affected: set[int] = set()
                    unlinked = 0
                    for charge, ownership in result.all():
                        if ownership is None:
                            unlinked += charge.due_usd_cents
                        elif charge.due_usd_cents > 0:
                            affected.add(ownership.user_id)
                    await session.flush()
                    for user_id in sorted(affected):
                        await reallocate_user(session, user_id)
                    return PublishedRun(
                        run_id=run.id,
                        affected_users=tuple(sorted(affected)),
                        unlinked_usd_cents=unlinked,
                    )

    async def discard(self, run_id: int) -> None:
        async with self._lock:
            async with self._sessions() as session:
                async with session.begin():
                    run = await session.get(BillingRun, run_id)
                    if run is None or run.status != "draft":
                        raise BillingError("Черновик не найден или уже опубликован")
                    await session.execute(delete(KeyCharge).where(KeyCharge.run_id == run_id))
                    await session.delete(run)
