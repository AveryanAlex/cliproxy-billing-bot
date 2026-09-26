from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import BillingRun, KeyCharge, KeyOwnership, Payment, PaymentAllocation, User, utc_now
from .money import rub_quote_remaining, settle_with_rubles


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class ChargeView:
    id: int
    start_date: date
    end_exclusive: date
    published_at: datetime | None
    key_label: str
    due_usd_cents: int
    due_rub_kopeks: int
    remaining_usd_cents: int
    remaining_rub_kopeks: int


@dataclass(frozen=True)
class PaymentView:
    id: int
    currency: str
    amount_minor: int
    status: str
    source: str
    submitted_at: datetime
    reviewed_at: datetime | None


@dataclass(frozen=True)
class Balance:
    due_usd_cents: int
    due_rub_kopeks: int
    credit_usd_cents: int
    credit_rub_kopeks: int
    charges: tuple[ChargeView, ...]
    payments: tuple[PaymentView, ...]


async def upsert_user(session: AsyncSession, telegram_id: int, display_name: str) -> None:
    user = await session.get(User, telegram_id)
    if user is None:
        session.add(User(telegram_id=telegram_id, display_name=display_name[:200]))
    else:
        user.display_name = display_name[:200]


async def link_key(session: AsyncSession, user_id: int, keeper_key_id: str, label: str) -> bool:
    """Link an active Keeper key. Returns False when the user already owns it."""
    existing = await session.get(KeyOwnership, keeper_key_id)
    if existing is not None:
        if existing.user_id == user_id:
            return False
        raise LedgerError("Этот ключ уже привязан к другому аккаунту. Обратитесь к администратору.")
    session.add(KeyOwnership(keeper_key_id=keeper_key_id, user_id=user_id, label=label[:200]))
    await session.flush()
    await reallocate_user(session, user_id)
    return True


async def user_keys(session: AsyncSession, user_id: int) -> tuple[KeyOwnership, ...]:
    result = await session.scalars(
        select(KeyOwnership)
        .where(KeyOwnership.user_id == user_id)
        .order_by(KeyOwnership.linked_at, KeyOwnership.keeper_key_id)
    )
    return tuple(result.all())


async def _user_charges(session: AsyncSession, user_id: int) -> list[tuple[KeyCharge, BillingRun]]:
    result = await session.execute(
        select(KeyCharge, BillingRun)
        .join(BillingRun, BillingRun.id == KeyCharge.run_id)
        .join(KeyOwnership, KeyOwnership.keeper_key_id == KeyCharge.keeper_key_id)
        .where(KeyOwnership.user_id == user_id, BillingRun.status == "published")
        .order_by(BillingRun.end_exclusive, BillingRun.id, KeyCharge.id)
    )
    return list(result.all())


async def _accepted_payments(session: AsyncSession, user_id: int) -> list[Payment]:
    result = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user_id, Payment.status == "accepted")
        .order_by(Payment.submitted_at, Payment.id)
    )
    return list(result.all())


async def reallocate_user(session: AsyncSession, user_id: int) -> None:
    """Rebuild FIFO allocations, including newly linked historical charges."""
    await session.execute(
        delete(PaymentAllocation).where(
            PaymentAllocation.payment_id.in_(select(Payment.id).where(Payment.user_id == user_id))
        )
    )
    await session.flush()
    charges = await _user_charges(session, user_id)
    remaining = {charge.id: charge.due_usd_cents for charge, _run in charges}

    for payment in await _accepted_payments(session, user_id):
        available = payment.amount_minor
        for charge, _run in charges:
            owed = remaining[charge.id]
            if available <= 0:
                break
            if owed <= 0:
                continue
            if payment.currency == "USD":
                settled = min(owed, available)
                consumed = settled
            elif payment.currency == "RUB":
                settled, consumed = settle_with_rubles(
                    charge.due_usd_cents, charge.due_rub_kopeks, owed, available
                )
            else:
                raise LedgerError(f"Неизвестная валюта платежа {payment.currency}")
            if settled <= 0:
                # Do not skip the oldest open charge.
                break
            session.add(
                PaymentAllocation(
                    payment_id=payment.id,
                    charge_id=charge.id,
                    source_minor=consumed,
                    settled_usd_cents=settled,
                )
            )
            remaining[charge.id] -= settled
            available -= consumed
    await session.flush()


async def get_balance(session: AsyncSession, user_id: int) -> Balance:
    charges = await _user_charges(session, user_id)
    allocations_result = await session.execute(
        select(PaymentAllocation, Payment)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(Payment.user_id == user_id)
    )
    settled_by_charge: dict[int, int] = {}
    used_by_payment: dict[int, int] = {}
    for allocation, payment in allocations_result.all():
        settled_by_charge[allocation.charge_id] = (
            settled_by_charge.get(allocation.charge_id, 0) + allocation.settled_usd_cents
        )
        used_by_payment[payment.id] = used_by_payment.get(payment.id, 0) + allocation.source_minor

    charge_views: list[ChargeView] = []
    for charge, run in charges:
        remaining = charge.due_usd_cents - settled_by_charge.get(charge.id, 0)
        if remaining < 0:
            raise LedgerError("Начисление оплачено сверх суммы")
        charge_views.append(
            ChargeView(
                id=charge.id,
                start_date=run.start_date,
                end_exclusive=run.end_exclusive,
                published_at=run.published_at,
                key_label=charge.key_label,
                due_usd_cents=charge.due_usd_cents,
                due_rub_kopeks=charge.due_rub_kopeks,
                remaining_usd_cents=remaining,
                remaining_rub_kopeks=rub_quote_remaining(
                    charge.due_usd_cents, charge.due_rub_kopeks, remaining
                ),
            )
        )

    payment_rows = await session.scalars(
        select(Payment).where(Payment.user_id == user_id).order_by(Payment.submitted_at, Payment.id)
    )
    payments = list(payment_rows.all())
    credits = {"USD": 0, "RUB": 0}
    for payment in payments:
        if payment.status == "accepted":
            credits[payment.currency] += payment.amount_minor - used_by_payment.get(payment.id, 0)
    if any(value < 0 for value in credits.values()):
        raise LedgerError("Платёж распределён сверх суммы")
    return Balance(
        due_usd_cents=sum(charge.remaining_usd_cents for charge in charge_views),
        due_rub_kopeks=sum(charge.remaining_rub_kopeks for charge in charge_views),
        credit_usd_cents=credits["USD"],
        credit_rub_kopeks=credits["RUB"],
        charges=tuple(charge_views),
        payments=tuple(
            PaymentView(
                id=payment.id,
                currency=payment.currency,
                amount_minor=payment.amount_minor,
                status=payment.status,
                source=payment.source,
                submitted_at=payment.submitted_at,
                reviewed_at=payment.reviewed_at,
            )
            for payment in payments
        ),
    )


async def submit_payment(
    session: AsyncSession,
    user_id: int,
    currency: str,
    amount_minor: int,
    *,
    screenshot_file_id: str | None,
    screenshot_kind: str | None,
    manual: bool = False,
    reviewed_by: int | None = None,
) -> Payment:
    if currency not in {"USD", "RUB"} or amount_minor <= 0:
        raise LedgerError("Неверная сумма или валюта")
    if manual and reviewed_by is None:
        raise LedgerError("Ручной платёж должен указать администратора")
    if not manual and (not screenshot_file_id or screenshot_kind not in {"photo", "document"}):
        raise LedgerError("Нужен скриншот оплаты")
    payment = Payment(
        user_id=user_id,
        currency=currency,
        amount_minor=amount_minor,
        status="accepted" if manual else "pending",
        source="manual" if manual else "screenshot",
        screenshot_file_id=screenshot_file_id,
        screenshot_kind=screenshot_kind,
        reviewed_at=utc_now() if manual else None,
        reviewed_by=reviewed_by,
    )
    session.add(payment)
    await session.flush()
    if manual:
        await reallocate_user(session, user_id)
    return payment


async def review_payment(
    session: AsyncSession, payment_id: int, admin_id: int, *, approve: bool
) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise LedgerError("Платёж не найден")
    if payment.status != "pending":
        raise LedgerError("Этот платёж уже проверен")
    payment.status = "accepted" if approve else "rejected"
    payment.reviewed_at = utc_now()
    payment.reviewed_by = admin_id
    await session.flush()
    if approve:
        await reallocate_user(session, payment.user_id)
    return payment
