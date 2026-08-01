"""Minor-unit money values and deterministic allocation helpers.

All public money is represented as an integer count of currency minor units.
``ExactAmount`` is deliberately separate: it retains a ``Fraction`` for
intermediate calculations until a caller chooses an explicit rounding policy.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
import re
from typing import TypeAlias, TypeVar

from .errors import (
    AllocationError,
    CurrencyMismatchError,
    EmptyAllocationError,
    InvalidCurrencyError,
    InvalidMoneyAmountError,
    InvalidWeightError,
)


_ISO_CURRENCY_CODE = re.compile(r"[A-Z]{3}")
_RecipientId = TypeVar("_RecipientId", bound=Hashable)
Allocation: TypeAlias = dict[Hashable, "Money"]


def _require_currency(currency: object) -> "Currency":
    if not isinstance(currency, Currency):
        raise TypeError("currency must be a Currency")
    return currency


def _require_integer(value: object, *, name: str) -> int:
    # bool is an int subclass, but is never a meaningful monetary amount.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMoneyAmountError(f"{name} must be an integer number of minor units")
    return value


@dataclass(frozen=True, slots=True)
class Currency:
    """ISO-style currency metadata used for validation and display.

    Arithmetic never uses ``exponent``: amounts are already expressed in
    minor units.
    """

    code: str
    exponent: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _ISO_CURRENCY_CODE.fullmatch(self.code) is None:
            raise InvalidCurrencyError("currency code must be a three-letter uppercase ISO code")
        if isinstance(self.exponent, bool) or not isinstance(self.exponent, int):
            raise InvalidCurrencyError("currency exponent must be an integer")
        if not 0 <= self.exponent <= 6:
            raise InvalidCurrencyError("currency exponent must be between 0 and 6")


@dataclass(frozen=True, slots=True, eq=False)
class Money:
    """A customer-visible integer amount in one currency's minor unit."""

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        _require_integer(self.amount, name="amount")
        _require_currency(self.currency)

    def _assert_same_currency(self, other: "Money | ExactAmount") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency.code} with {other.currency.code}"
            )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Money) and (self.amount, self.currency) == (
            other.amount,
            other.currency,
        )

    def __hash__(self) -> int:
        return hash((self.amount, self.currency))

    def __add__(self, other: object) -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._assert_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: object) -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._assert_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __pos__(self) -> "Money":
        return self

    def _compare(self, other: object, operator: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented  # type: ignore[return-value]
        self._assert_same_currency(other)
        return operator(self.amount, other.amount)  # type: ignore[operator]

    def __lt__(self, other: object) -> bool:
        return self._compare(other, lambda left, right: left < right)

    def __le__(self, other: object) -> bool:
        return self._compare(other, lambda left, right: left <= right)

    def __gt__(self, other: object) -> bool:
        return self._compare(other, lambda left, right: left > right)

    def __ge__(self, other: object) -> bool:
        return self._compare(other, lambda left, right: left >= right)

    def as_exact(self) -> "ExactAmount":
        """Return this amount for use in exact intermediate calculations."""
        return ExactAmount(Fraction(self.amount), self.currency)


@dataclass(frozen=True, slots=True, eq=False)
class ExactAmount:
    """A fractional minor-unit amount for internal, pre-rounding arithmetic."""

    amount: Fraction
    currency: Currency

    def __post_init__(self) -> None:
        if isinstance(self.amount, float):
            raise InvalidMoneyAmountError("exact amounts cannot be constructed from floats")
        if isinstance(self.amount, bool) or not isinstance(self.amount, (int, Fraction)):
            raise InvalidMoneyAmountError("exact amount must be an int or Fraction")
        _require_currency(self.currency)
        object.__setattr__(self, "amount", Fraction(self.amount))

    @classmethod
    def from_money(cls, money: Money) -> "ExactAmount":
        return cls(Fraction(money.amount), money.currency)

    def _assert_same_currency(self, other: "Money | ExactAmount") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency.code} with {other.currency.code}"
            )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ExactAmount) and (self.amount, self.currency) == (
            other.amount,
            other.currency,
        )

    def __hash__(self) -> int:
        return hash((self.amount, self.currency))

    def __add__(self, other: object) -> "ExactAmount":
        if isinstance(other, Money):
            other = other.as_exact()
        if not isinstance(other, ExactAmount):
            return NotImplemented
        self._assert_same_currency(other)
        return ExactAmount(self.amount + other.amount, self.currency)

    def __sub__(self, other: object) -> "ExactAmount":
        if isinstance(other, Money):
            other = other.as_exact()
        if not isinstance(other, ExactAmount):
            return NotImplemented
        self._assert_same_currency(other)
        return ExactAmount(self.amount - other.amount, self.currency)

    def __mul__(self, factor: object) -> "ExactAmount":
        if isinstance(factor, bool) or not isinstance(factor, (int, Fraction)):
            return NotImplemented
        return ExactAmount(self.amount * Fraction(factor), self.currency)

    def __rmul__(self, factor: object) -> "ExactAmount":
        return self * factor

    def __truediv__(self, divisor: object) -> "ExactAmount":
        if isinstance(divisor, bool) or not isinstance(divisor, (int, Fraction)):
            return NotImplemented
        return ExactAmount(self.amount / Fraction(divisor), self.currency)


def _validate_recipients(recipient_ids: Sequence[_RecipientId]) -> list[_RecipientId]:
    if isinstance(recipient_ids, (str, bytes)) or not isinstance(recipient_ids, Sequence):
        raise AllocationError("recipient_ids must be an ordered sequence")
    recipients = list(recipient_ids)
    if not recipients:
        raise EmptyAllocationError("cannot allocate to zero recipients")
    if len(set(recipients)) != len(recipients):
        raise AllocationError("recipient IDs must be unique")
    return recipients


def allocate_equal(total: Money, recipient_ids: Sequence[_RecipientId]) -> dict[_RecipientId, Money]:
    """Split ``total`` equally, giving remainders to recipients in input order."""
    if not isinstance(total, Money):
        raise TypeError("total must be Money")
    recipients = _validate_recipients(recipient_ids)
    share, remainder = divmod(total.amount, len(recipients))
    return {
        recipient: Money(share + (1 if index < remainder else 0), total.currency)
        for index, recipient in enumerate(recipients)
    }


def allocate_weighted(
    total: Money,
    recipient_ids: Sequence[_RecipientId] | Mapping[_RecipientId, int],
    weights: Sequence[int] | None = None,
) -> dict[_RecipientId, Money]:
    """Allocate by positive integer weights using ordered largest remainders.

    Pass ordered ``recipient_ids`` and a same-length ``weights`` sequence.  As
    a convenience, an insertion-ordered mapping of recipient ID to weight may
    be passed as the second argument with ``weights`` omitted.
    """
    if not isinstance(total, Money):
        raise TypeError("total must be Money")
    if weights is None:
        if not isinstance(recipient_ids, Mapping):
            raise AllocationError("weights are required unless recipient weights are a mapping")
        recipients = _validate_recipients(list(recipient_ids.keys()))
        raw_weights = list(recipient_ids.values())
    else:
        recipients = _validate_recipients(recipient_ids)  # type: ignore[arg-type]
        if isinstance(weights, (str, bytes)) or not isinstance(weights, Sequence):
            raise InvalidWeightError("weights must be an ordered sequence")
        raw_weights = list(weights)

    if len(recipients) != len(raw_weights):
        raise AllocationError("recipient_ids and weights must have the same length")
    if any(isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0 for weight in raw_weights):
        raise InvalidWeightError("weights must be positive integers")

    weight_total = sum(raw_weights)
    quotients: list[int] = []
    remainders: list[int] = []
    for weight in raw_weights:
        quotient, remainder = divmod(total.amount * weight, weight_total)
        quotients.append(quotient)
        remainders.append(remainder)

    units_left = total.amount - sum(quotients)
    # Sorting by index makes equal fractional remainders deterministic in the
    # caller's canonical order. This also handles negative totals correctly:
    # floors are raised toward zero where required to reconcile the total.
    winners = sorted(range(len(recipients)), key=lambda index: (-remainders[index], index))
    extras = set(winners[:units_left])
    return {
        recipient: Money(quotients[index] + (index in extras), total.currency)
        for index, recipient in enumerate(recipients)
    }
