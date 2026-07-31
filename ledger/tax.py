"""Exact, bucketed tax calculation for invoices.

Tax is deliberately calculated from exact minor-unit amounts and rounded only
after all taxable amounts in a rate/jurisdiction/treatment bucket are added.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from math import ceil, floor

from .errors import InvalidTaxError
from .money import Currency, ExactAmount, Money


class TaxTreatment(str, Enum):
    TAXABLE = "taxable"
    EXEMPT = "exempt"
    ZERO_RATED = "zero_rated"
    REVERSE_CHARGE = "reverse_charge"


def round_half_up(amount: ExactAmount) -> Money:
    """Round an exact minor-unit amount, with halves away from zero."""
    if not isinstance(amount, ExactAmount):
        raise TypeError("amount must be an ExactAmount")
    value = amount.amount
    rounded = floor(value + Fraction(1, 2)) if value >= 0 else ceil(value - Fraction(1, 2))
    return Money(rounded, amount.currency)


@dataclass(frozen=True, slots=True)
class TaxBucket:
    """A snapshot of one tax calculation bucket.

    ``rate`` is a fraction (``Fraction(19, 100)`` for 19%), never a float.
    """

    jurisdiction: str
    rate: Fraction
    treatment: TaxTreatment = TaxTreatment.TAXABLE

    def __post_init__(self) -> None:
        if not isinstance(self.jurisdiction, str) or not self.jurisdiction:
            raise InvalidTaxError("jurisdiction must be a non-empty string")
        if isinstance(self.rate, float) or isinstance(self.rate, bool) or not isinstance(
            self.rate, (int, Fraction)
        ):
            raise InvalidTaxError("tax rate must be an int or Fraction, never a float")
        rate = Fraction(self.rate)
        if rate < 0:
            raise InvalidTaxError("tax rate cannot be negative")
        if not isinstance(self.treatment, TaxTreatment):
            raise InvalidTaxError("tax treatment must be a TaxTreatment")
        object.__setattr__(self, "rate", rate)

    @property
    def is_taxable(self) -> bool:
        return self.treatment is TaxTreatment.TAXABLE and self.rate != 0


@dataclass(frozen=True, slots=True)
class TaxCalculation:
    bucket: TaxBucket
    taxable_base: ExactAmount
    exact_tax: ExactAmount
    tax: Money


def calculate_tax(bucket: TaxBucket, taxable_base: ExactAmount) -> TaxCalculation:
    """Calculate one bucket's tax and round exactly once, half up."""
    if not isinstance(bucket, TaxBucket):
        raise TypeError("bucket must be a TaxBucket")
    if not isinstance(taxable_base, ExactAmount):
        raise TypeError("taxable_base must be an ExactAmount")
    exact_tax = taxable_base * bucket.rate if bucket.is_taxable else ExactAmount(0, taxable_base.currency)
    return TaxCalculation(bucket, taxable_base, exact_tax, round_half_up(exact_tax))


def calculate_taxes(
    bases: dict[TaxBucket, ExactAmount], *, currency: Currency | None = None
) -> tuple[TaxCalculation, ...]:
    """Calculate tax for every supplied, already-aggregated bucket."""
    calculations: list[TaxCalculation] = []
    for bucket, base in bases.items():
        if currency is not None and base.currency != currency:
            raise InvalidTaxError("all tax bases must use the invoice currency")
        calculations.append(calculate_tax(bucket, base))
    return tuple(calculations)
