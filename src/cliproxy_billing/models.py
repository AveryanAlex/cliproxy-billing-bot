from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class KeyOwnership(Base):
    __tablename__ = "key_ownerships"

    keeper_key_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    label: Mapped[str] = mapped_column(String(200))
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BillingRun(Base):
    __tablename__ = "billing_runs"
    __table_args__ = (UniqueConstraint("start_date", "end_exclusive"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_exclusive: Mapped[date] = mapped_column(Date)
    subscription_usd_cents: Mapped[int] = mapped_column(Integer)
    fee_percent: Mapped[str] = mapped_column(String(40))
    rub_per_usd: Mapped[str] = mapped_column(String(40))
    total_usd_cents: Mapped[int] = mapped_column(Integer)
    total_rub_kopeks: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KeyCharge(Base):
    __tablename__ = "key_charges"
    __table_args__ = (
        UniqueConstraint("run_id", "keeper_key_id"),
        Index("ix_key_charges_keeper_key_id", "keeper_key_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("billing_runs.id", ondelete="CASCADE"))
    keeper_key_id: Mapped[str] = mapped_column(String(100))
    key_label: Mapped[str] = mapped_column(String(200))
    usage_cost_usd: Mapped[str] = mapped_column(String(50))
    requests: Mapped[int] = mapped_column(Integer)
    principal_usd_cents: Mapped[int] = mapped_column(Integer)
    fee_usd_cents: Mapped[int] = mapped_column(Integer)
    due_usd_cents: Mapped[int] = mapped_column(Integer)
    due_rub_kopeks: Mapped[int] = mapped_column(Integer)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))
    screenshot_file_id: Mapped[str | None] = mapped_column(String(500))
    screenshot_kind: Mapped[str | None] = mapped_column(String(20))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(Integer)


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    __table_args__ = (UniqueConstraint("payment_id", "charge_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"))
    charge_id: Mapped[int] = mapped_column(ForeignKey("key_charges.id", ondelete="CASCADE"))
    source_minor: Mapped[int] = mapped_column(Integer)
    settled_usd_cents: Mapped[int] = mapped_column(Integer)
