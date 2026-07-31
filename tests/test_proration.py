from datetime import datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

import pytest

from ledger.errors import CurrencyMismatchError, InvalidProrationError
from ledger.money import Currency, ExactAmount, Money
from ledger.periods import BillingCadence
from ledger.plans import Plan
from ledger.proration import PlanChangeTiming, quote_proration
from ledger.subscriptions import Activate, Subscription, SubscriptionState, transition


UTC = ZoneInfo("UTC")
USD = Currency("USD")
EUR = Currency("EUR")
START = datetime(2026, 1, 1, 10, tzinfo=UTC)


def plan(identifier: str, amount: int, cadence: BillingCadence = BillingCadence.MONTHLY, currency: Currency = USD) -> Plan:
    return Plan(identifier, identifier.title(), Money(amount, currency), cadence)


def active() -> Subscription:
    subscription = Subscription("sub_1", plan("basic", 1001).snapshot(), SubscriptionState.INCOMPLETE, UTC)
    return transition(subscription, Activate("activate"), START).subscription


def microseconds(delta: timedelta) -> int:
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def test_midpoint_change_has_exact_proportional_credit_charge_and_net():
    subscription = active()
    midpoint = subscription.current_period.start + (subscription.current_period.end - subscription.current_period.start) / 2
    quote = quote_proration(subscription, plan("pro", 2003), midpoint)

    assert quote.credit == ExactAmount(Fraction(-1001, 2), USD)
    assert quote.charge == ExactAmount(Fraction(2003, 2), USD)
    assert quote.net == ExactAmount(Fraction(1002, 2), USD)


def test_change_at_period_start_credits_full_old_amount_and_charges_full_new_amount():
    subscription = active()
    quote = quote_proration(subscription, plan("pro", 2003), subscription.current_period.start)

    assert quote.credit == ExactAmount(-1001, USD)
    assert quote.charge == ExactAmount(2003, USD)


def test_change_at_period_end_is_deferred_and_has_no_proration():
    subscription = active()
    quote = quote_proration(subscription, plan("pro", 2003), subscription.current_period.end)

    assert quote.is_deferred
    assert quote.effective_at == subscription.current_period.end
    assert quote.credit == ExactAmount(0, USD)
    assert quote.charge == ExactAmount(0, USD)


@pytest.mark.parametrize("old_amount", range(0, 10))
@pytest.mark.parametrize("new_amount", range(0, 10))
@pytest.mark.parametrize("elapsed_days", (0, 1, 7, 14, 30))
def test_all_price_and_time_combinations_conserve_exact_value(old_amount, new_amount, elapsed_days):
    subscription = Subscription("sub_1", plan("old", old_amount).snapshot(), SubscriptionState.INCOMPLETE, UTC)
    subscription = transition(subscription, Activate("activate"), START).subscription
    changed_at = min(subscription.current_period.start + timedelta(days=elapsed_days), subscription.current_period.end - timedelta(microseconds=1))
    quote = quote_proration(subscription, plan("new", new_amount), changed_at)
    ratio = Fraction(
        microseconds(subscription.current_period.end - changed_at),
        microseconds(subscription.current_period.end - subscription.current_period.start),
    )

    assert quote.credit + subscription.plan.price.as_exact() * ratio == ExactAmount(0, USD)
    assert quote.charge - Money(new_amount, USD).as_exact() * ratio == ExactAmount(0, USD)


def test_requested_deferred_change_and_cross_cadence_change_leave_current_period_untouched():
    subscription = active()
    midperiod = subscription.current_period.start + timedelta(days=10)
    requested = quote_proration(subscription, plan("pro", 2000), midperiod, timing=PlanChangeTiming.AT_PERIOD_END)
    cross_cadence = quote_proration(subscription, plan("annual", 20_000, BillingCadence.YEARLY), midperiod)

    assert requested.is_deferred and cross_cadence.is_deferred
    assert requested.net == ExactAmount(0, USD)
    assert cross_cadence.net == ExactAmount(0, USD)


def test_immediate_currency_change_and_out_of_period_change_are_rejected():
    subscription = active()
    with pytest.raises(CurrencyMismatchError):
        quote_proration(subscription, plan("eu", 1000, currency=EUR), subscription.current_period.start)
    with pytest.raises(InvalidProrationError):
        quote_proration(subscription, plan("pro", 2000), subscription.current_period.start - timedelta(microseconds=1))
