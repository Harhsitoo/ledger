"""Pure subscription state transitions and entitlement decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TypeAlias
from zoneinfo import ZoneInfo

from .errors import InvalidSubscriptionError, InvalidTransitionError
from .periods import CycleAnchor, Period, period_for_cycle
from .plans import PlanPriceSnapshot
from .time import require_aware_datetime, require_zoneinfo


class SubscriptionState(str, Enum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCEL_SCHEDULED = "cancel_scheduled"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class Subscription:
    """A subscription plus only the intervals required to derive access."""

    id: str
    plan: PlanPriceSnapshot
    state: SubscriptionState
    billing_zone: ZoneInfo
    trial_started_at: datetime | None = None
    trial_ends_at: datetime | None = None
    current_period: Period | None = None
    cycle_anchor: CycleAnchor | None = None
    canceled_at: datetime | None = None
    last_transition_at: datetime | None = None
    last_idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise InvalidSubscriptionError("subscription id must be a non-empty string")
        if not isinstance(self.plan, PlanPriceSnapshot):
            raise InvalidSubscriptionError("plan must be a PlanPriceSnapshot")
        if not isinstance(self.state, SubscriptionState):
            raise InvalidSubscriptionError("state must be a SubscriptionState")
        require_zoneinfo(self.billing_zone, name="billing_zone")
        for name in ("trial_started_at", "trial_ends_at", "canceled_at", "last_transition_at"):
            value = getattr(self, name)
            if value is not None:
                require_aware_datetime(value, name=name)
        if self.last_idempotency_key is not None and (not isinstance(self.last_idempotency_key, str) or not self.last_idempotency_key):
            raise InvalidSubscriptionError("last_idempotency_key must be a non-empty string")
        if (self.trial_started_at is None) != (self.trial_ends_at is None):
            raise InvalidSubscriptionError("trial start and end must be supplied together")
        if self.trial_started_at is not None and self.trial_ends_at <= self.trial_started_at:
            raise InvalidSubscriptionError("trial must end after it starts")
        paid = self.state in {SubscriptionState.ACTIVE, SubscriptionState.PAST_DUE, SubscriptionState.CANCEL_SCHEDULED}
        if paid and (self.current_period is None or self.cycle_anchor is None):
            raise InvalidSubscriptionError("paid states require a current period and cycle anchor")
        if self.state is SubscriptionState.TRIALING and self.trial_started_at is None:
            raise InvalidSubscriptionError("trialing state requires a trial interval")
        if self.state is SubscriptionState.CANCELED and self.canceled_at is None:
            raise InvalidSubscriptionError("canceled state requires canceled_at")


@dataclass(frozen=True, slots=True)
class DomainEvent:
    type: str
    subscription_id: str
    occurred_at: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class TransitionResult:
    subscription: Subscription
    events: tuple[DomainEvent, ...]

    @property
    def entity(self) -> Subscription:
        return self.subscription


@dataclass(frozen=True, slots=True)
class Activate:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class TrialExpired:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MarkPastDue:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PaymentSucceeded:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CancelAtPeriodEnd:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CancelImmediately:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class Reactivate:
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PeriodEnded:
    """Apply an already-scheduled cancellation at its period boundary."""

    idempotency_key: str


SubscriptionCommand: TypeAlias = Activate | TrialExpired | MarkPastDue | PaymentSucceeded | CancelAtPeriodEnd | CancelImmediately | Reactivate | PeriodEnded


def entitled_at(subscription: Subscription, instant: datetime) -> bool:
    """Whether access is granted at ``instant`` under half-open intervals."""
    if not isinstance(subscription, Subscription):
        raise TypeError("subscription must be a Subscription")
    require_aware_datetime(instant, name="instant")
    if subscription.state is SubscriptionState.TRIALING:
        return subscription.trial_started_at <= instant < subscription.trial_ends_at  # type: ignore[operator]
    if subscription.state in {SubscriptionState.ACTIVE, SubscriptionState.PAST_DUE, SubscriptionState.CANCEL_SCHEDULED}:
        return subscription.current_period.contains(instant)  # type: ignore[union-attr]
    return False


is_entitled = entitled_at


def transition(subscription: Subscription, command: SubscriptionCommand, at: datetime) -> TransitionResult:
    """Apply one explicit command at one explicit instant."""
    if not isinstance(subscription, Subscription):
        raise TypeError("subscription must be a Subscription")
    if not isinstance(command, (Activate, TrialExpired, MarkPastDue, PaymentSucceeded, CancelAtPeriodEnd, CancelImmediately, Reactivate, PeriodEnded)):
        raise TypeError("command must be a subscription command")
    require_aware_datetime(at, name="at")
    if not isinstance(command.idempotency_key, str) or not command.idempotency_key:
        raise InvalidTransitionError("idempotency_key must be a non-empty string")
    if subscription.last_idempotency_key == command.idempotency_key:
        return TransitionResult(subscription, ())

    if isinstance(command, TrialExpired):
        if subscription.state is not SubscriptionState.TRIALING or at != subscription.trial_ends_at:
            raise InvalidTransitionError("a trial may expire only at its trial end")
        return _paid(subscription, at, SubscriptionState.ACTIVE, command, "trial_expired")
    if isinstance(command, Activate):
        if subscription.state is not SubscriptionState.INCOMPLETE:
            raise InvalidTransitionError("only incomplete subscriptions can be activated")
        return _paid(subscription, at, SubscriptionState.ACTIVE, command, "subscription_activated")
    if isinstance(command, MarkPastDue):
        if subscription.state is not SubscriptionState.ACTIVE:
            raise InvalidTransitionError("only active subscriptions can become past due")
        _require_current(subscription, at)
        return _result(subscription, SubscriptionState.PAST_DUE, at, command, "subscription_past_due")
    if isinstance(command, PaymentSucceeded):
        if subscription.state is not SubscriptionState.PAST_DUE:
            raise InvalidTransitionError("only past-due subscriptions can be restored by payment")
        _require_current(subscription, at)
        return _result(subscription, SubscriptionState.ACTIVE, at, command, "subscription_activated")
    if isinstance(command, CancelAtPeriodEnd):
        if subscription.state not in {SubscriptionState.ACTIVE, SubscriptionState.PAST_DUE}:
            raise InvalidTransitionError("only paid subscriptions can be scheduled for cancellation")
        _require_current(subscription, at)
        return _result(subscription, SubscriptionState.CANCEL_SCHEDULED, at, command, "cancellation_scheduled")
    if isinstance(command, CancelImmediately):
        if subscription.state not in {SubscriptionState.TRIALING, SubscriptionState.ACTIVE, SubscriptionState.PAST_DUE, SubscriptionState.CANCEL_SCHEDULED}:
            raise InvalidTransitionError("subscription cannot be canceled from its current state")
        if not entitled_at(subscription, at):
            raise InvalidTransitionError("immediate cancellation must occur while entitled")
        replacement = replace(subscription, state=SubscriptionState.CANCELED, canceled_at=at, last_transition_at=at, last_idempotency_key=command.idempotency_key)
        return TransitionResult(replacement, (_event(replacement, "subscription_canceled", at, command),))
    if isinstance(command, Reactivate):
        if subscription.state is SubscriptionState.CANCEL_SCHEDULED:
            _require_current(subscription, at)
            return _result(subscription, SubscriptionState.ACTIVE, at, command, "cancellation_reverted")
        if subscription.state is SubscriptionState.CANCELED:
            return _paid(subscription, at, SubscriptionState.ACTIVE, command, "subscription_reactivated")
        raise InvalidTransitionError("only canceled subscriptions can be reactivated")
    if isinstance(command, PeriodEnded):
        if subscription.state is not SubscriptionState.CANCEL_SCHEDULED or at != subscription.current_period.end:
            raise InvalidTransitionError("only a scheduled cancellation may end at its period boundary")
        replacement = replace(subscription, state=SubscriptionState.CANCELED, canceled_at=at, last_transition_at=at, last_idempotency_key=command.idempotency_key)
        return TransitionResult(replacement, (_event(replacement, "subscription_canceled", at, command),))
    raise AssertionError("unreachable")


def _paid(subscription: Subscription, at: datetime, state: SubscriptionState, command: SubscriptionCommand, event_type: str) -> TransitionResult:
    anchor = CycleAnchor.from_start(at, subscription.billing_zone)
    period = period_for_cycle(anchor, subscription.plan.cadence, 0)
    replacement = replace(subscription, state=state, current_period=period, cycle_anchor=anchor, trial_started_at=None, trial_ends_at=None, canceled_at=None, last_transition_at=at, last_idempotency_key=command.idempotency_key)
    return TransitionResult(replacement, (_event(replacement, event_type, at, command),))


def _result(subscription: Subscription, state: SubscriptionState, at: datetime, command: SubscriptionCommand, event_type: str) -> TransitionResult:
    replacement = replace(subscription, state=state, last_transition_at=at, last_idempotency_key=command.idempotency_key)
    return TransitionResult(replacement, (_event(replacement, event_type, at, command),))


def _event(subscription: Subscription, event_type: str, at: datetime, command: SubscriptionCommand) -> DomainEvent:
    return DomainEvent(event_type, subscription.id, at, command.idempotency_key)


def _require_current(subscription: Subscription, at: datetime) -> None:
    if subscription.current_period is None or at not in subscription.current_period:
        raise InvalidTransitionError("transition must occur in the current paid period")
