from decimal import Decimal

import pytest

from cliproxy_billing.money import (
    allocate_largest_remainder,
    parse_minor,
    rub_quote_remaining,
    settle_with_rubles,
)


def test_allocation_is_exact_and_zero_key_gets_zero() -> None:
    result = allocate_largest_remainder(
        40000, {"a": Decimal("30"), "b": Decimal("10"), "unused": Decimal("0")}
    )
    assert result == {"a": 30000, "b": 10000, "unused": 0}
    assert sum(result.values()) == 40000


def test_allocation_distributes_odd_cents_deterministically() -> None:
    result = allocate_largest_remainder(
        2, {"a": Decimal("1"), "b": Decimal("1"), "c": Decimal("1")}
    )
    assert result == {"a": 1, "b": 1, "c": 0}


def test_ruble_partial_settlement_uses_original_quote() -> None:
    settled, consumed = settle_with_rubles(1010, 80800, 1010, 40000)
    assert (settled, consumed) == (500, 40000)
    assert rub_quote_remaining(1010, 80800, 510) == 40800


def test_money_input_rejects_fractional_kopeks() -> None:
    with pytest.raises(ValueError, match="двух знаков"):
        parse_minor("1.001", name="Сумма")
