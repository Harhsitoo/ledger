"""Deterministic retry scheduling and outcome transitions for invoices."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import InvalidDunningError, InvalidDunningTransitionError
from .invoices import Invoice, InvoiceStatus
from .payments import PaymentAttempt, PaymentOutcome
from .subscriptions import (
    CancelImmediately,
    MarkPastDue,
    PaymentSucceeded,
    Subscription,
    SubscriptionState,
    entitled_at,
    transition as transition_subscription,
)
from .time import require_aware_datetime


DEFAULT_RETRY_DELAYS = (
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
    timedelta(days=14),
)
UTC = ZoneInfo("UTC")


@dataclass(frozen=True, slots=True)
class DunningPolicy:
    """A versioned initial-at-due collection attempt and bounded UTC retries."""

    version: str = "default-v1"
    retry_delays: tuple[timedelta, ...] = DEFAULT_RETRY_DELAYS

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidDunningError("policy version must be a non-empty string")
        if not isinstance(self.retry_delays, tuple):
            raise InvalidDunningError("retry_delays must be a tuple")
        previous: timedelta | None = None
        for delay in self.retry_delays:
            if not isinstance(delay, timedelta) or delay <= timedelta():
                raise InvalidDunningError("retry delays must be positive durations")
            if previous is not None and delay <= previous:
                raise InvalidDunningError("retry delays must be strictly increasing")
            previous = delay

    @property
    def attempt_count(self) -> int:
        return 1 + len(self.retry_delays)

    def attempt_at(self, due_at: datetime, attempt_number: int) -> datetime:
        """Return a UTC scheduled instant for a one-based attempt number."""
        require_aware_datetime(due_at, name="due_at")
        if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or not 1 <= attempt_number <= self.attempt_count:
            raise InvalidDunningError("attempt_number is outside this policy's schedule")
        due_utc = due_at.astimezone(UTC)
        return due_utc if attempt_number == 1 else due_utc + self.retry_delays[attempt_number - 2]

    def schedule(self, due_at: datetime) -> tuple[datetime, ...]:
        """Return initial attempt plus each retry, all expressed in UTC."""
        return tuple(self.attempt_at(due_at, number) for number in range(1, self.attempt_count + 1))


@dataclass(frozen=True, slots=True)
class DunningCase:
    """An open case owns exactly one currently scheduled payment attempt."""

    invoice_id: str
    policy: DunningPolicy
    due_at: datetime
    next_attempt_number: int | None = 1
    next_attempt_at: datetime | None = None
    closed_at: datetime | None = None
    exhausted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.invoice_id, str) or not self.invoice_id:
            raise InvalidDunningError("invoice_id must be a non-empty string")
        if not isinstance(self.policy, DunningPolicy):
            raise InvalidDunningError("policy must be a DunningPolicy")
        require_aware_datetime(self.due_at, name="due_at")
        if self.closed_at is not None:
            require_aware_datetime(self.closed_at, name="closed_at")
        open_case = self.closed_at is None
        if open_case and self.next_attempt_number is None:
            raise InvalidDunningError("open cases have one scheduled attempt")
        if not open_case and (self.next_attempt_number is not None or self.next_attempt_at is not None):
            raise InvalidDunningError("closed cases have no scheduled attempt")
        if self.exhausted and open_case:
            raise InvalidDunningError("an exhausted case must be closed")
        if open_case:
            assert self.next_attempt_number is not None
            expected = self.policy.attempt_at(self.due_at, self.next_attempt_number)
            if self.next_attempt_at is None:
                object.__setattr__(self, "next_attempt_at", expected)
            elif self.next_attempt_at != expected:
                raise InvalidDunningError("next_attempt_at must match the policy schedule")

    @classmethod
    def open(cls, invoice_id: str, policy: DunningPolicy, due_at: datetime) -> "DunningCase":
        return cls(invoice_id, policy, due_at, 1, policy.attempt_at(due_at, 1))

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True, slots=True)
class DunningResult:
    """Replacement values after accepting one payment-adapter outcome."""

    case: DunningCase
    invoice: Invoice
    subscription: Subscription


def start_dunning(invoice: Invoice, policy: DunningPolicy, due_at: datetime) -> DunningCase:
    """Create the case whose initial collection attempt is due immediately."""
    if not isinstance(invoice, Invoice):
        raise TypeError("invoice must be an Invoice")
    if invoice.status is not InvoiceStatus.OPEN:
        raise InvalidDunningError("only open invoices can enter dunning")
    return DunningCase.open(invoice.id, policy, due_at)


def apply_payment_attempt(case: DunningCase, invoice: Invoice, subscription: Subscription, attempt: PaymentAttempt, at: datetime) -> DunningResult:
    """Apply precisely the currently scheduled payment result.

    A closed case rejects all later outcomes, including a late success after
    exhaustion.  The application may record that provider event separately,
    but it cannot reopen an uncollectible invoice or resurrect entitlement.
    """
    if not isinstance(case, DunningCase) or not isinstance(invoice, Invoice) or not isinstance(subscription, Subscription):
        raise TypeError("case, invoice, and subscription must be Ledger values")
    if not isinstance(attempt, PaymentAttempt):
        raise TypeError("attempt must be a PaymentAttempt")
    require_aware_datetime(at, name="at")
    if invoice.id != case.invoice_id or attempt.invoice_id != case.invoice_id:
        raise InvalidDunningTransitionError("attempt and invoice must belong to the dunning case")
    if invoice.status is not InvoiceStatus.OPEN:
        raise InvalidDunningTransitionError("only open invoices can receive dunning attempts")
    if not case.is_open or attempt.attempt_number != case.next_attempt_number:
        raise InvalidDunningTransitionError("attempt is not the currently scheduled attempt")
    assert case.next_attempt_at is not None
    if at != case.next_attempt_at:
        raise InvalidDunningTransitionError("attempt must be reported at its scheduled instant")

    if attempt.outcome is PaymentOutcome.SUCCEEDED:
        restored = _restore_if_current(subscription, at, attempt.idempotency_key)
        return DunningResult(_closed(case, at, exhausted=False), replace(invoice, status=InvoiceStatus.PAID), restored)
    if attempt.outcome is PaymentOutcome.TERMINAL_FAILURE or attempt.attempt_number == case.policy.attempt_count:
        canceled = _cancel_for_exhaustion(subscription, at, attempt.idempotency_key)
        return DunningResult(_closed(case, at, exhausted=True), replace(invoice, status=InvoiceStatus.UNCOLLECTIBLE), canceled)

    past_due = _mark_past_due(subscription, at, attempt.idempotency_key)
    next_number = attempt.attempt_number + 1
    next_at = case.policy.attempt_at(case.due_at, next_number)
    return DunningResult(replace(case, next_attempt_number=next_number, next_attempt_at=next_at), invoice, past_due)


def _closed(case: DunningCase, at: datetime, *, exhausted: bool) -> DunningCase:
    return replace(case, next_attempt_number=None, next_attempt_at=None, closed_at=at, exhausted=exhausted)


def _mark_past_due(subscription: Subscription, at: datetime, key: str) -> Subscription:
    if subscription.state is SubscriptionState.ACTIVE and entitled_at(subscription, at):
        return transition_subscription(subscription, MarkPastDue(key), at).subscription
    return subscription


def _restore_if_current(subscription: Subscription, at: datetime, key: str) -> Subscription:
    if subscription.state is SubscriptionState.PAST_DUE and entitled_at(subscription, at):
        return transition_subscription(subscription, PaymentSucceeded(key), at).subscription
    return subscription


def _cancel_for_exhaustion(subscription: Subscription, at: datetime, key: str) -> Subscription:
    if subscription.state in {SubscriptionState.ACTIVE, SubscriptionState.PAST_DUE, SubscriptionState.CANCEL_SCHEDULED} and entitled_at(subscription, at):
        return transition_subscription(subscription, CancelImmediately(key), at).subscription
    return subscription


# A short functional spelling consistent with subscriptions.transition.
transition = apply_payment_attempt
