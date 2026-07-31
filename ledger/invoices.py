"""Draft and finalized invoices with exact-to-visible reconciliation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction
from math import floor
from typing import Iterable

from .errors import CurrencyMismatchError, InvalidInvoiceError, InvoiceFinalizedError
from .money import Currency, ExactAmount, Money
from .tax import TaxBucket, TaxTreatment, calculate_tax, round_half_up


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class InvoiceLineCategory(str, Enum):
    RECURRING_CHARGE = "recurring_charge"
    PRORATION_CHARGE = "proration_charge"
    PRORATION_CREDIT = "proration_credit"
    DISCOUNT = "discount"
    TAX = "tax"
    CREDIT_BALANCE_APPLICATION = "credit_balance_application"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInvoiceError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """One exact draft line or materialized finalized line."""

    id: str
    description: str
    category: InvoiceLineCategory
    currency: Currency
    exact_amount: ExactAmount
    tax_treatment: TaxTreatment = TaxTreatment.EXEMPT
    tax_bucket: TaxBucket | None = None
    amount: Money | None = None

    def __post_init__(self) -> None:
        _text(self.id, "line id")
        _text(self.description, "line description")
        if not isinstance(self.category, InvoiceLineCategory):
            raise InvalidInvoiceError("line category must be an InvoiceLineCategory")
        if not isinstance(self.currency, Currency) or not isinstance(self.exact_amount, ExactAmount):
            raise InvalidInvoiceError("line currency and exact amount are required")
        if self.exact_amount.currency != self.currency:
            raise CurrencyMismatchError("line exact amount must use the line currency")
        if not isinstance(self.tax_treatment, TaxTreatment):
            raise InvalidInvoiceError("line tax treatment must be a TaxTreatment")
        if self.tax_bucket is not None and not isinstance(self.tax_bucket, TaxBucket):
            raise InvalidInvoiceError("line tax bucket must be a TaxBucket")
        if self.tax_bucket is not None and self.tax_bucket.treatment != self.tax_treatment:
            raise InvalidInvoiceError("line tax bucket and tax treatment must agree")
        if self.category is InvoiceLineCategory.TAX and self.tax_bucket is None:
            raise InvalidInvoiceError("tax lines require a tax bucket")
        if self.amount is not None and (not isinstance(self.amount, Money) or self.amount.currency != self.currency):
            raise InvalidInvoiceError("materialized amount must use the line currency")


@dataclass(frozen=True, slots=True)
class Invoice:
    """An immutable value object; draft edits return a replacement invoice."""

    id: str
    currency: Currency
    lines: tuple[InvoiceLine, ...] = ()
    status: InvoiceStatus = InvoiceStatus.DRAFT
    subtotal: Money | None = None
    tax_total: Money | None = None
    total: Money | None = None

    def __post_init__(self) -> None:
        _text(self.id, "invoice id")
        if not isinstance(self.currency, Currency) or not isinstance(self.status, InvoiceStatus):
            raise InvalidInvoiceError("invoice currency and status are required")
        if not isinstance(self.lines, tuple):
            raise InvalidInvoiceError("invoice lines must be a tuple")
        ids = [line.id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise InvalidInvoiceError("invoice line IDs must be unique")
        if any(not isinstance(line, InvoiceLine) or line.currency != self.currency for line in self.lines):
            raise InvalidInvoiceError("all invoice lines must use the invoice currency")
        totals = (self.subtotal, self.tax_total, self.total)
        if self.status is InvoiceStatus.DRAFT:
            if any(value is not None for value in totals) or any(line.amount is not None for line in self.lines):
                raise InvalidInvoiceError("draft invoices cannot have materialized amounts")
        else:
            if any(not isinstance(value, Money) or value.currency != self.currency for value in totals):
                raise InvalidInvoiceError("finalized invoices require money totals")
            if any(line.amount is None for line in self.lines):
                raise InvalidInvoiceError("finalized invoices require materialized line amounts")
            self.assert_reconciles()

    @property
    def is_finalized(self) -> bool:
        return self.status is not InvoiceStatus.DRAFT

    def add_line(self, line: InvoiceLine) -> "Invoice":
        """Return a draft replacement containing ``line``."""
        self._require_draft()
        if not isinstance(line, InvoiceLine):
            raise TypeError("line must be an InvoiceLine")
        if line.currency != self.currency:
            raise CurrencyMismatchError("line currency must match invoice currency")
        if line.id in {existing.id for existing in self.lines}:
            raise InvalidInvoiceError("invoice line IDs must be unique")
        return replace(self, lines=(*self.lines, line))

    def finalize(self, *, settled: bool = False) -> "Invoice":
        """Materialize an invoice. Repeating finalization is idempotent."""
        if self.is_finalized:
            return self
        materialized_non_tax, subtotal = _materialize_non_tax_lines(self.lines, self.currency)
        tax_lines, tax_total = _materialize_tax_lines(self.lines, self.currency)
        status = InvoiceStatus.PAID if settled else InvoiceStatus.OPEN
        return Invoice(
            self.id,
            self.currency,
            (*materialized_non_tax, *tax_lines),
            status,
            subtotal,
            tax_total,
            subtotal + tax_total,
        )

    def assert_reconciles(self) -> None:
        if not self.is_finalized:
            raise InvalidInvoiceError("a draft invoice has no visible totals to reconcile")
        assert self.subtotal is not None and self.tax_total is not None and self.total is not None
        non_tax = sum(line.amount.amount for line in self.lines if line.category is not InvoiceLineCategory.TAX)  # type: ignore[union-attr]
        tax = sum(line.amount.amount for line in self.lines if line.category is InvoiceLineCategory.TAX)  # type: ignore[union-attr]
        if non_tax != self.subtotal.amount or tax != self.tax_total.amount or self.total != self.subtotal + self.tax_total:
            raise InvalidInvoiceError("invoice materialized lines do not reconcile to totals")

    def _require_draft(self) -> None:
        if self.is_finalized:
            raise InvoiceFinalizedError("a finalized invoice is immutable; create a correction invoice")


def finalize(invoice: Invoice, *, settled: bool = False) -> Invoice:
    """Functional spelling of :meth:`Invoice.finalize`."""
    if not isinstance(invoice, Invoice):
        raise TypeError("invoice must be an Invoice")
    return invoice.finalize(settled=settled)


def _materialize_non_tax_lines(lines: tuple[InvoiceLine, ...], currency: Currency) -> tuple[tuple[InvoiceLine, ...], Money]:
    non_tax = tuple(line for line in lines if line.category is not InvoiceLineCategory.TAX)
    subtotal = round_half_up(ExactAmount(sum((line.exact_amount.amount for line in non_tax), Fraction()), currency))
    return _allocate_visible_amounts(non_tax, subtotal), subtotal


def _allocate_visible_amounts(lines: tuple[InvoiceLine, ...], total: Money) -> tuple[InvoiceLine, ...]:
    floors = [floor(line.exact_amount.amount) for line in lines]
    units = total.amount - sum(floors)
    # A deterministic largest-remainder allocation.  Fractional ties use line ID,
    # not draft insertion order, so replay is independent of storage ordering.
    winners = sorted(range(len(lines)), key=lambda index: (-(lines[index].exact_amount.amount - floors[index]), lines[index].id))
    selected = set(winners[:units])
    return tuple(replace(line, amount=Money(floors[index] + (index in selected), total.currency)) for index, line in enumerate(lines))


def _materialize_tax_lines(lines: tuple[InvoiceLine, ...], currency: Currency) -> tuple[tuple[InvoiceLine, ...], Money]:
    bases: dict[TaxBucket, Fraction] = defaultdict(Fraction)
    for line in lines:
        if line.category is InvoiceLineCategory.TAX or line.tax_bucket is None:
            continue
        bases[line.tax_bucket] += line.exact_amount.amount
    materialized: list[InvoiceLine] = []
    for bucket in sorted(bases, key=lambda item: (item.jurisdiction, item.rate, item.treatment.value)):
        calculation = calculate_tax(bucket, ExactAmount(bases[bucket], currency))
        if calculation.tax.amount == 0:
            continue
        materialized.append(InvoiceLine(
            id=f"tax:{bucket.jurisdiction}:{bucket.rate.numerator}/{bucket.rate.denominator}:{bucket.treatment.value}",
            description=f"Tax ({bucket.jurisdiction})",
            category=InvoiceLineCategory.TAX,
            currency=currency,
            exact_amount=calculation.exact_tax,
            tax_treatment=bucket.treatment,
            tax_bucket=bucket,
            amount=calculation.tax,
        ))
    tax_total = Money(sum(line.amount.amount for line in materialized), currency)  # type: ignore[union-attr]
    return tuple(materialized), tax_total
