from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .ledger import Balance
from .money import money_text

BALANCE_BUTTON = "💰 Баланс"
HISTORY_BUTTON = "📜 История"
KEYS_BUTTON = "🔑 Мои ключи"
ADD_KEY_BUTTON = "➕ Добавить ключ"
PAY_BUTTON = "📷 Оплатить"
ADMIN_BUTTON = "⚙️ Управление"


def main_keyboard(*, is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BALANCE_BUTTON), KeyboardButton(text=HISTORY_BUTTON)],
        [KeyboardButton(text=KEYS_BUTTON), KeyboardButton(text=ADD_KEY_BUTTON)],
        [KeyboardButton(text=PAY_BUTTON)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def buttons(*rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def user_menu() -> InlineKeyboardMarkup:
    return buttons(
        (("💰 Баланс", "user:balance"), ("📜 История", "user:history")),
        (("🔑 Мои ключи", "user:keys"), ("➕ Добавить ключ", "user:add_key")),
        (("📷 Оплатить", "pay:start"),),
    )


def balance_text(balance: Balance) -> str:
    lines = [
        "Баланс",
        f"Долг: {money_text(balance.due_usd_cents, 'USD')} "
        f"или {money_text(balance.due_rub_kopeks, 'RUB')}",
    ]
    if balance.credit_usd_cents or balance.credit_rub_kopeks:
        lines.append(
            f"Аванс: {money_text(balance.credit_usd_cents, 'USD')} и "
            f"{money_text(balance.credit_rub_kopeks, 'RUB')}"
        )
    if balance.due_usd_cents and balance.credit_rub_kopeks:
        lines.append(
            "Небольшой остаток аванса может сосуществовать с долгом из-за округления "
            "до целых центов."
        )
    lines.append("Рублёвый долг сложен по курсам соответствующих начислений.")
    return "\n".join(lines)


def local_time(value: datetime | None, time_zone: ZoneInfo) -> str:
    if value is None:
        return "время неизвестно"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(time_zone).strftime("%Y-%m-%d %H:%M")


def history_text(balance: Balance, time_zone: ZoneInfo) -> str:
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for charge in balance.charges:
        end_day = charge.end_exclusive - timedelta(days=1)
        key = (
            charge.start_date.isoformat(),
            end_day.isoformat(),
            local_time(charge.published_at, time_zone),
        )
        values = groups[key]
        values[0] += charge.due_usd_cents
        values[1] += charge.due_rub_kopeks
        values[2] += charge.remaining_usd_cents
        values[3] += charge.remaining_rub_kopeks
    lines = ["Начисления:"]
    for (start, end, issued), (due_usd, due_rub, remaining_usd, _remaining_rub) in groups.items():
        status = (
            "оплачено"
            if remaining_usd == 0
            else ("частично оплачено" if remaining_usd < due_usd else "не оплачено")
        )
        lines.append(
            f"• {start} — {end} (выставлено {issued}): {money_text(due_usd, 'USD')} / "
            f"{money_text(due_rub, 'RUB')} · {status}"
        )
    if not groups:
        lines.append("Начислений пока нет.")
    lines.append("")
    lines.append("Платежи:")
    status_labels = {"pending": "на проверке", "accepted": "подтверждён", "rejected": "отклонён"}
    for payment in balance.payments:
        lines.append(
            f"• #{payment.id} · {local_time(payment.submitted_at, time_zone)}: "
            f"{money_text(payment.amount_minor, payment.currency)} · "
            f"{status_labels.get(payment.status, payment.status)}"
        )
    if not balance.payments:
        lines.append("Платежей пока нет.")
    return "\n".join(lines)


async def send_text(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Split long reports to stay under Telegram's message limit."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > 3500:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(line[index : index + 3500] for index in range(0, len(line), 3500))
            continue
        if len(current) + len(line) > 3500:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks):
        await bot.send_message(
            chat_id,
            chunk,
            reply_markup=markup if index == len(chunks) - 1 else None,
        )
