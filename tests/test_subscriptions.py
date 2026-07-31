from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ledger.errors import InvalidTransitionError
from ledger.money import Currency, Money
from ledger.periods import BillingCadence
from ledger.plans import Plan, PlanPriceSnapshot
from ledger.subscriptions import (
    Activate,
    CancelAtPeriodEnd,
    CancelImmediately,
    MarkPastDue,
    PaymentSucceeded,
    PeriodEnded,
    Reactivate,
    Subscription,
    SubscriptionState,
    TrialExpired,
    entitled_at,
    transition,
)


UTC = ZoneInfo("UTC")
START = datetime(2026, 1, 1, 10, tzinfo=UTC)
USD = Currency("USD")
SNAPSHOT = PlanPriceSnapshot.from_plan(Plan("basic", "Basic", Money(1000, USD), BillingCadence.MONTHLY))


def incomplete() -> Subscription:
    return Subscription("sub_1", SNAPSHOT, SubscriptionState.INCOMPLETE, UTC)


def trial() -> Subscription:
    return Subscription("sub_1", SNAPSHOT, SubscriptionState.TRIALING, UTC, START, START + timedelta(days=14))


def active() -> Subscription:
    return transition(incomplete(), Activate("activate"), START).subscription


def test_plan_price_snapshot_is_a_value_copy_that_cannot_be_rewritten():
    plan = Plan("basic", "Basic", Money(1000, USD), BillingCadence.MONTHLY)
    snapshot = plan.snapshot()
    changed_plan = replace(plan, price=Money(2500, USD))

    assert snapshot.price == Money(1000, USD)
    assert changed_plan.snapshot().price == Money(2500, USD)
    with pytest.raises(FrozenInstanceError):
        snapshot.price = Money(1, USD)  # type: ignore[misc]


def test_trial_expiry_starts_first_paid_period_exactly_at_trial_end():
    subscription = trial()
    ended = transition(subscription, TrialExpired("trial-expired"), subscription.trial_ends_at)

    assert ended.subscription.state is SubscriptionState.ACTIVE
    assert ended.subscription.current_period.start == subscription.trial_ends_at
    assert ended.subscription.cycle_anchor.local_date == subscription.trial_ends_at.date()
    assert ended.events[0].type == "trial_expired"


def test_activation_of_incomplete_starts_a_paid_cycle():
    result = transition(incomplete(), Activate("paid"), START)
    assert result.subscription.state is SubscriptionState.ACTIVE
    assert result.subscription.current_period.start == START
    assert result.events[0].type == "subscription_activated"


def test_active_past_due_active_payment_transitions_are_legal():
    subscription = active()
    overdue = transition(subscription, MarkPastDue("failed"), START + timedelta(days=1))
    restored = transition(overdue.subscription, PaymentSucceeded("paid"), START + timedelta(days=2))

    assert overdue.subscription.state is SubscriptionState.PAST_DUE
    assert overdue.events[0].type == "subscription_past_due"
    assert restored.subscription.state is SubscriptionState.ACTIVE
    assert restored.events[0].type == "subscription_activated"


@pytest.mark.parametrize("factory, command", [
    (incomplete, TrialExpired("x")),
    (incomplete, MarkPastDue("x")),
    (incomplete, PaymentSucceeded("x")),
    (incomplete, CancelAtPeriodEnd("x")),
    (incomplete, CancelImmediately("x")),
    (incomplete, Reactivate("x")),
    (trial, Activate("x")),
    (trial, MarkPastDue("x")),
    (trial, PaymentSucceeded("x")),
    (trial, CancelAtPeriodEnd("x")),
    (trial, Reactivate("x")),
    (active, Activate("x")),
    (active, TrialExpired("x")),
    (active, PaymentSucceeded("x")),
    (active, Reactivate("x")),
])
def test_illegal_transitions_from_incomplete_trial_and_active_raise(factory, command):
    with pytest.raises(InvalidTransitionError):
        transition(factory(), command, START + timedelta(days=1))


def test_trial_expiry_at_any_instant_other_than_explicit_end_is_illegal():
    subscription = trial()
    with pytest.raises(InvalidTransitionError):
        transition(subscription, TrialExpired("early"), subscription.trial_ends_at - timedelta(microseconds=1))
    with pytest.raises(InvalidTransitionError):
        transition(subscription, TrialExpired("late"), subscription.trial_ends_at + timedelta(microseconds=1))


def test_cancel_at_period_end_preserves_access_and_reactivation_reverts_it():
    subscription = active()
    at = START + timedelta(days=10)
    scheduled = transition(subscription, CancelAtPeriodEnd("schedule"), at)

    assert scheduled.subscription.state is SubscriptionState.CANCEL_SCHEDULED
    assert entitled_at(scheduled.subscription, scheduled.subscription.current_period.end - timedelta(microseconds=1))
    assert not entitled_at(scheduled.subscription, scheduled.subscription.current_period.end)

    restored = transition(scheduled.subscription, Reactivate("undo"), at + timedelta(days=1))
    assert restored.subscription.state is SubscriptionState.ACTIVE
    assert restored.subscription.current_period == scheduled.subscription.current_period
    assert restored.events[0].type == "cancellation_reverted"


def test_scheduled_cancellation_becomes_canceled_only_at_its_period_end():
    scheduled = transition(active(), CancelAtPeriodEnd("schedule"), START + timedelta(days=1)).subscription
    canceled = transition(scheduled, PeriodEnded("period-ended"), scheduled.current_period.end)
    assert canceled.subscription.state is SubscriptionState.CANCELED
    assert canceled.subscription.canceled_at == scheduled.current_period.end

    scheduled = transition(active(), CancelAtPeriodEnd("again"), START + timedelta(days=1)).subscription
    with pytest.raises(InvalidTransitionError):
        transition(scheduled, PeriodEnded("early"), scheduled.current_period.end - timedelta(microseconds=1))


def test_immediate_cancellation_ends_access_at_its_exact_instant_without_refund_event():
    subscription = active()
    at = START + timedelta(days=10)
    canceled = transition(subscription, CancelImmediately("cancel-now"), at)

    assert canceled.subscription.state is SubscriptionState.CANCELED
    assert canceled.subscription.canceled_at == at
    assert not entitled_at(canceled.subscription, at - timedelta(microseconds=1))
    assert not entitled_at(canceled.subscription, at)
    assert canceled.events[0].type == "subscription_canceled"


def test_reactivating_canceled_starts_a_new_cycle_and_does_not_resurrect_old_one():
    old = active()
    canceled = transition(old, CancelImmediately("cancel"), START + timedelta(days=5)).subscription
    restarted_at = START + timedelta(days=40)
    restarted = transition(canceled, Reactivate("restart"), restarted_at)

    assert restarted.subscription.state is SubscriptionState.ACTIVE
    assert restarted.subscription.current_period.start == restarted_at
    assert restarted.subscription.cycle_anchor != old.cycle_anchor
    assert restarted.subscription.trial_started_at is None
    assert restarted.events[0].type == "subscription_reactivated"


@pytest.mark.parametrize("factory, command", [
    (lambda: transition(active(), CancelAtPeriodEnd("s"), START + timedelta(days=1)).subscription, Activate("x")),
    (lambda: transition(active(), CancelAtPeriodEnd("s"), START + timedelta(days=1)).subscription, TrialExpired("x")),
    (lambda: transition(active(), CancelAtPeriodEnd("s"), START + timedelta(days=1)).subscription, MarkPastDue("x")),
    (lambda: transition(active(), CancelAtPeriodEnd("s"), START + timedelta(days=1)).subscription, PaymentSucceeded("x")),
    (lambda: transition(active(), CancelAtPeriodEnd("s"), START + timedelta(days=1)).subscription, CancelAtPeriodEnd("x")),
    (lambda: transition(active(), CancelImmediately("c"), START + timedelta(days=1)).subscription, Activate("x")),
    (lambda: transition(active(), CancelImmediately("c"), START + timedelta(days=1)).subscription, TrialExpired("x")),
    (lambda: transition(active(), CancelImmediately("c"), START + timedelta(days=1)).subscription, MarkPastDue("x")),
    (lambda: transition(active(), CancelImmediately("c"), START + timedelta(days=1)).subscription, PaymentSucceeded("x")),
    (lambda: transition(active(), CancelImmediately("c"), START + timedelta(days=1)).subscription, CancelAtPeriodEnd("x")),
    (lambda: transition(active(), CancelImmediately("c"), START + timedelta(days=1)).subscription, CancelImmediately("x")),
])
def test_illegal_transitions_from_scheduled_and_canceled_raise(factory, command):
    with pytest.raises(InvalidTransitionError):
        transition(factory(), command, START + timedelta(days=2))


@pytest.mark.parametrize("state_factory, command", [
    (active, MarkPastDue("late")),
    (active, CancelAtPeriodEnd("late")),
    (lambda: transition(active(), MarkPastDue("due"), START + timedelta(days=1)).subscription, PaymentSucceeded("late")),
    (lambda: transition(active(), CancelAtPeriodEnd("scheduled"), START + timedelta(days=1)).subscription, Reactivate("late")),
])
def test_paid_state_transitions_are_illegal_at_the_exclusive_period_end(state_factory, command):
    subscription = state_factory()
    with pytest.raises(InvalidTransitionError):
        transition(subscription, command, subscription.current_period.end)


def test_trial_and_paid_entitlement_use_half_open_boundaries():
    subscription = trial()
    assert entitled_at(subscription, START)
    assert entitled_at(subscription, subscription.trial_ends_at - timedelta(microseconds=1))
    assert not entitled_at(subscription, subscription.trial_ends_at)

    paid = transition(subscription, TrialExpired("expire"), subscription.trial_ends_at).subscription
    assert entitled_at(paid, paid.current_period.start)
    assert entitled_at(paid, paid.current_period.end - timedelta(microseconds=1))
    assert not entitled_at(paid, paid.current_period.end)


def test_replaying_an_idempotency_key_is_a_noop():
    activated = transition(incomplete(), Activate("one"), START)
    replay = transition(activated.subscription, Activate("one"), START + timedelta(days=1))
    assert replay.subscription == activated.subscription
    assert replay.events == ()
