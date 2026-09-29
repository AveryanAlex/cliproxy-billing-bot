from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

HUNDRED = Decimal("100")


def parse_positive_decimal(value: str, *, name: str, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(value.strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"{name}: неверное число") from error
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise ValueError(f"{name}: требуется положительное конечное число")
    return result


def to_minor(value: Decimal) -> int:
    return int((value * HUNDRED).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(value: int) -> Decimal:
    return Decimal(value) / HUNDRED


def money_text(value: int, currency: str) -> str:
    amount = f"{from_minor(value):,.2f}".replace(",", " ")
    return "$" + amount if currency == "USD" else f"{amount} ₽"


def suggested_payment_minor(due_minor: int, currency: str) -> int:
    if currency == "USD":
        return max(due_minor, 0)
    if currency != "RUB":
        raise ValueError(f"Неизвестная валюта {currency}")
    if due_minor <= 0:
        return 0
    hundred_rubles_in_kopeks = 10_000
    return (
        (due_minor + hundred_rubles_in_kopeks - 1) // hundred_rubles_in_kopeks
    ) * hundred_rubles_in_kopeks


def parse_minor(value: str, *, name: str) -> int:
    parsed = parse_positive_decimal(value, name=name)
    if parsed * 100 != (parsed * 100).to_integral_value():
        raise ValueError(f"{name}: не больше двух знаков после запятой")
    minor = to_minor(parsed)
    if minor <= 0:
        raise ValueError(f"{name}: сумма должна быть больше нуля")
    return minor


def allocate_largest_remainder(total_minor: int, weights: dict[str, Decimal]) -> dict[str, int]:
    if total_minor < 0 or not weights:
        raise ValueError("Нет получателей для распределения")
    total_weight = sum(weights.values(), Decimal(0))
    if total_weight <= 0:
        raise ValueError("Общее использование равно нулю")
    allocated: dict[str, int] = {}
    fractions: list[tuple[Decimal, str]] = []
    for key_id, weight in weights.items():
        if weight < 0:
            raise ValueError("Отрицательное использование")
        exact = Decimal(total_minor) * weight / total_weight
        whole = int(exact.to_integral_value(rounding=ROUND_DOWN))
        allocated[key_id] = whole
        fractions.append((exact - whole, key_id))
    remainder = total_minor - sum(allocated.values())
    for _fraction, key_id in sorted(fractions, key=lambda pair: (-pair[0], pair[1]))[:remainder]:
        allocated[key_id] += 1
    return allocated


def rub_quote_remaining(charge_usd_cents: int, charge_rub_kopeks: int, remaining: int) -> int:
    if charge_usd_cents <= 0 or remaining <= 0:
        return 0
    return to_minor(from_minor(charge_rub_kopeks) * Decimal(remaining) / Decimal(charge_usd_cents))


def settle_with_rubles(
    charge_usd_cents: int,
    charge_rub_kopeks: int,
    remaining_usd_cents: int,
    available_kopeks: int,
) -> tuple[int, int]:
    """Return USD cents retired and RUB kopeks consumed from this charge."""
    if remaining_usd_cents <= 0 or available_kopeks <= 0:
        return 0, 0
    before = rub_quote_remaining(charge_usd_cents, charge_rub_kopeks, remaining_usd_cents)
    if available_kopeks >= before:
        return remaining_usd_cents, before

    low, high = 0, remaining_usd_cents
    while low < high:
        middle = (low + high + 1) // 2
        after = rub_quote_remaining(
            charge_usd_cents, charge_rub_kopeks, remaining_usd_cents - middle
        )
        consumed = before - after
        if consumed <= available_kopeks:
            low = middle
        else:
            high = middle - 1
    after = rub_quote_remaining(charge_usd_cents, charge_rub_kopeks, remaining_usd_cents - low)
    return low, before - after
