from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ledger.dunning import DunningPolicy, apply_payment_attempt, start_dunning
from ledger.errors import InvalidDunningTransitionError
from ledger.invoices import Invoice, InvoiceLine, InvoiceLineCategory, InvoiceStatus
from ledger.money import Currency, ExactAmount, Money
from ledger.payments import PaymentAttempt, PaymentOutcome
from ledger.periods import BillingCadence
from ledger.plans import Plan, PlanPriceSnapshot
from ledger.subscriptions import Activate, Subscription, SubscriptionState, transition


UTC = ZoneInfo("UTC")
DUE = datetime(2026, 3, 1, 9, tzinfo=UTC)
USD = Currency("USD")
SNAPSHOT = PlanPriceSnapshot.from_plan(Plan("basic", "Basic", Money(1000, USD), BillingCadence.MONTHLY))


def open_invoice() -> Invoice:
    draft = Invoice("inv_1", USD).add_line(InvoiceLine("charge", "charge", InvoiceLineCategory.RECURRING_CHARGE, USD, ExactAmount(1000, USD)))
    return draft.finalize()


def active() -> Subscription:
    incomplete = Subscription("sub_1", SNAPSHOT, SubscriptionState.INCOMPLETE, UTC)
    return transition(incomplete, Activate("activate"), DUE).subscription


def attempt(number: int, outcome: PaymentOutcome) -> PaymentAttempt:
    return PaymentAttempt("inv_1", number, f"inv_1:{number}", outcome)


def test_full_retry_schedule_uses_utc_duration_backoff_and_exhausts_after_final_attempt():
    policy = DunningPolicy()
    case = start_dunning(open_invoice(), policy, DUE)
    assert policy.schedule(DUE) == tuple(DUE + timedelta(days=days) for days in (0, 1, 3, 7, 14))

    invoice = open_invoice()
    subscription = active()
    for number in range(1, 5):
        result = apply_payment_attempt(case, invoice, subscription, attempt(number, PaymentOutcome.RETRIABLE_FAILURE), case.next_attempt_at)
        case, invoice, subscription = result.case, result.invoice, result.subscription
        assert case.next_attempt_number == number + 1
        assert case.next_attempt_at == DUE + timedelta(days=(1, 3, 7, 14)[number - 1])

    exhausted = apply_payment_attempt(case, invoice, subscription, attempt(5, PaymentOutcome.RETRIABLE_FAILURE), case.next_attempt_at)
    assert exhausted.case.exhausted and not exhausted.case.is_open
    assert exhausted.invoice.status is InvoiceStatus.UNCOLLECTIBLE
    assert exhausted.subscription.state is SubscriptionState.CANCELED
    assert exhausted.subscription.canceled_at == DUE + timedelta(days=14)


def test_success_partway_through_closes_case_pays_invoice_and_restores_subscription():
    case = start_dunning(open_invoice(), DunningPolicy(), DUE)
    first = apply_payment_attempt(case, open_invoice(), active(), attempt(1, PaymentOutcome.RETRIABLE_FAILURE), DUE)
    recovered = apply_payment_attempt(first.case, first.invoice, first.subscription, attempt(2, PaymentOutcome.SUCCEEDED), first.case.next_attempt_at)

    assert recovered.case.is_open is False
    assert recovered.case.exhausted is False
    assert recovered.invoice.status is InvoiceStatus.PAID
    assert recovered.subscription.state is SubscriptionState.ACTIVE


def test_terminal_failure_exhausts_without_consuming_remaining_retries():
    case = start_dunning(open_invoice(), DunningPolicy(), DUE)
    result = apply_payment_attempt(case, open_invoice(), active(), attempt(1, PaymentOutcome.TERMINAL_FAILURE), DUE)

    assert result.case.exhausted
    assert result.invoice.status is InvoiceStatus.UNCOLLECTIBLE
    assert result.subscription.state is SubscriptionState.CANCELED


def test_late_success_after_exhaustion_is_rejected_and_cannot_revive_entitlement():
    case = start_dunning(open_invoice(), DunningPolicy(retry_delays=()), DUE)
    exhausted = apply_payment_attempt(case, open_invoice(), active(), attempt(1, PaymentOutcome.RETRIABLE_FAILURE), DUE)

    with pytest.raises(InvalidDunningTransitionError):
        apply_payment_attempt(exhausted.case, exhausted.invoice, exhausted.subscription, attempt(1, PaymentOutcome.SUCCEEDED), DUE)
