from fractions import Fraction

import pytest

from ledger.errors import (
    AllocationError,
    CurrencyMismatchError,
    EmptyAllocationError,
    InvalidMoneyAmountError,
    InvalidWeightError,
)
from ledger.money import Currency, ExactAmount, Money, allocate_equal, allocate_weighted


USD = Currency("USD", 2)
JPY = Currency("JPY", 0)


def amounts(allocation):
    return [money.amount for money in allocation.values()]


def test_money_uses_integer_minor_units_only():
    assert Money(1099, USD).amount == 1099
    for value in (10.0, Fraction(1, 2), True, "1099"):
        with pytest.raises(InvalidMoneyAmountError):
            Money(value, USD)


def test_currency_matched_money_arithmetic_and_comparison():
    assert Money(1099, USD) + Money(1, USD) == Money(1100, USD)
    assert Money(1099, USD) - Money(1100, USD) == Money(-1, USD)
    assert Money(1, USD) < Money(2, USD)
    for operation in (
        lambda: Money(1, USD) + Money(1, JPY),
        lambda: Money(1, USD) - Money(1, JPY),
        lambda: Money(1, USD) < Money(1, JPY),
    ):
        with pytest.raises(CurrencyMismatchError):
            operation()


def test_exact_amount_keeps_fractional_minor_units_without_float_inputs():
    exact = ExactAmount(Fraction(1, 3), USD)
    assert exact + Money(1, USD) == ExactAmount(Fraction(4, 3), USD)
    assert exact * 3 == ExactAmount(1, USD)
    with pytest.raises(InvalidMoneyAmountError):
        ExactAmount(0.5, USD)
    with pytest.raises(CurrencyMismatchError):
        _ = exact + ExactAmount(Fraction(1, 3), JPY)


def test_equal_allocation_remainder_follows_caller_order_for_positive_and_negative_totals():
    recipients = ["third", "first", "second"]
    assert amounts(allocate_equal(Money(100, USD), recipients)) == [34, 33, 33]
    assert amounts(allocate_equal(Money(-5, USD), recipients)) == [-1, -2, -2]


@pytest.mark.parametrize("total_amount", range(-25, 26))
@pytest.mark.parametrize("count", range(1, 10))
def test_equal_allocation_conserves_every_minor_unit(total_amount, count):
    allocation = allocate_equal(Money(total_amount, USD), list(range(count)))
    values = amounts(allocation)
    assert sum(values) == total_amount
    assert max(values) - min(values) <= 1


def test_weighted_allocation_uses_largest_remainder_and_input_order_for_ties():
    allocation = allocate_weighted(Money(10, USD), ["b", "a", "c"], [1, 1, 1])
    assert list(allocation) == ["b", "a", "c"]
    assert amounts(allocation) == [4, 3, 3]
    assert amounts(allocate_weighted(Money(-5, USD), ["a", "b"], [1, 2])) == [-2, -3]


@pytest.mark.parametrize("total_amount", range(-25, 26))
@pytest.mark.parametrize("count", range(1, 8))
def test_weighted_allocation_conserves_every_minor_unit(total_amount, count):
    weights = list(range(1, count + 1))
    allocation = allocate_weighted(Money(total_amount, USD), list(range(count)), weights)
    assert sum(amounts(allocation)) == total_amount


def test_weighted_allocation_accepts_ordered_recipient_weight_mapping():
    allocation = allocate_weighted(Money(7, USD), {"z": 2, "a": 1})
    assert list(allocation) == ["z", "a"]
    assert amounts(allocation) == [5, 2]


def test_allocation_rejects_zero_recipients_duplicate_ids_and_invalid_weights():
    with pytest.raises(EmptyAllocationError):
        allocate_equal(Money(1, USD), [])
    with pytest.raises(EmptyAllocationError):
        allocate_weighted(Money(1, USD), [], [])
    with pytest.raises(AllocationError):
        allocate_equal(Money(1, USD), ["a", "a"])
    with pytest.raises(InvalidWeightError):
        allocate_weighted(Money(1, USD), ["a"], [0])
