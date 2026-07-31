from fractions import Fraction

import pytest

from ledger.errors import InvoiceFinalizedError
from ledger.invoices import Invoice, InvoiceLine, InvoiceLineCategory, InvoiceStatus
from ledger.money import Currency, ExactAmount, Money
from ledger.tax import TaxBucket, TaxTreatment


USD = Currency("USD")
VAT = TaxBucket("GB", Fraction(1, 5), TaxTreatment.TAXABLE)


def line(identifier, amount, *, bucket=None):
    return InvoiceLine(identifier, identifier, InvoiceLineCategory.RECURRING_CHARGE, USD, ExactAmount(amount, USD), TaxTreatment.TAXABLE if bucket else TaxTreatment.EXEMPT, bucket)


def test_final_invoice_reconciles_materialized_lines_subtotal_tax_and_total_exactly():
    invoice = Invoice("inv_1", USD).add_line(line("a", Fraction(101, 10), bucket=VAT)).add_line(line("b", Fraction(101, 10), bucket=VAT)).finalize()

    assert invoice.status is InvoiceStatus.OPEN
    assert sum(item.amount.amount for item in invoice.lines if item.category is not InvoiceLineCategory.TAX) == invoice.subtotal.amount
    assert sum(item.amount.amount for item in invoice.lines if item.category is InvoiceLineCategory.TAX) == invoice.tax_total.amount
    assert invoice.total == invoice.subtotal + invoice.tax_total
    assert invoice.subtotal == Money(20, USD)
    assert invoice.tax_total == Money(4, USD)


def test_many_small_line_items_round_once_without_accumulated_drift():
    invoice = Invoice("inv_micro", USD)
    for index in range(100):
        invoice = invoice.add_line(line(f"line_{index:03}", Fraction(1, 100)))

    final = invoice.finalize()
    assert final.subtotal == Money(1, USD)
    assert sum(item.amount.amount for item in final.lines) == 1


def test_finalizing_twice_is_idempotent():
    final = Invoice("inv_2", USD).add_line(line("charge", 1)).finalize()
    assert final.finalize() is final


def test_mutating_finalized_invoice_raises():
    final = Invoice("inv_3", USD).add_line(line("charge", 1)).finalize()
    with pytest.raises(InvoiceFinalizedError):
        final.add_line(line("another", 1))
