from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
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
CANCEL_BUTTON = "❌ Отмена"
BACK_BUTTON = "⬅️ Главное меню"
USD_BUTTON = "💵 USD"
RUB_BUTTON = "₽ RUB"
PAY_SUGGESTED_PREFIX = "💳 Оплатить "
ADMIN_NEW_BUTTON = "🧮 Новый расчёт"
ADMIN_DRAFT_BUTTON = "📋 Черновик"
ADMIN_PENDING_BUTTON = "💳 Проверить оплаты"
ADMIN_USERS_BUTTON = "👥 Участники"
ADMIN_UNLINKED_BUTTON = "🔑 Непривязанные начисления"
ADMIN_MANUAL_BUTTON = "➕ Занести платёж / аванс"
ADMIN_ACTION_BUTTONS = {
    ADMIN_NEW_BUTTON,
    ADMIN_DRAFT_BUTTON,
    ADMIN_PENDING_BUTTON,
    ADMIN_USERS_BUTTON,
    ADMIN_UNLINKED_BUTTON,
    ADMIN_MANUAL_BUTTON,
}
DRAFT_PUBLISH_BUTTON = "✅ Опубликовать расчёт"
DRAFT_DISCARD_BUTTON = "🗑 Удалить черновик"


def reply_keyboard(*rows: tuple[str, ...]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_keyboard(*, is_admin: bool) -> ReplyKeyboardMarkup:
    rows: list[tuple[str, ...]] = [
        (BALANCE_BUTTON, HISTORY_BUTTON),
        (KEYS_BUTTON, ADD_KEY_BUTTON),
        (PAY_BUTTON,),
    ]
    if is_admin:
        rows.append((ADMIN_BUTTON,))
    return reply_keyboard(*rows)


def admin_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard(
        (ADMIN_NEW_BUTTON, ADMIN_DRAFT_BUTTON),
        (ADMIN_PENDING_BUTTON, ADMIN_USERS_BUTTON),
        (ADMIN_UNLINKED_BUTTON,),
        (ADMIN_MANUAL_BUTTON,),
        (BACK_BUTTON,),
    )


def currency_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard((USD_BUTTON, RUB_BUTTON), (CANCEL_BUTTON,))


def suggested_payment_text(amount_minor: int, currency: str) -> str:
    if currency == "RUB":
        rubles = f"{amount_minor // 100:,}".replace(",", " ")
        return f"{rubles} ₽"
    return money_text(amount_minor, currency)


def suggested_payment_button(amount_minor: int, currency: str) -> str:
    return PAY_SUGGESTED_PREFIX + suggested_payment_text(amount_minor, currency)


def payment_choice_keyboard(*, amount_minor: int, currency: str) -> ReplyKeyboardMarkup:
    rows: list[tuple[str, ...]] = []
    if amount_minor > 0:
        rows.append((suggested_payment_button(amount_minor, currency),))
    rows.append((CANCEL_BUTTON,))
    return reply_keyboard(*rows)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard((CANCEL_BUTTON,))


def draft_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard(
        (DRAFT_PUBLISH_BUTTON,),
        (DRAFT_DISCARD_BUTTON,),
        (CANCEL_BUTTON,),
    )


def buttons(*rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
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
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    parse_mode: ParseMode | None = None,
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
            parse_mode=parse_mode,
        )
