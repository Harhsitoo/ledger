"""Exact, boundary-aware quotes for subscription plan changes.

Proration deliberately produces :class:`~ledger.money.ExactAmount` values.
Invoice finalization is the only layer that rounds them into customer-visible
``Money`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from fractions import Fraction

from .errors import CurrencyMismatchError, InvalidProrationError
from .money import ExactAmount
from .plans import Plan, PlanPriceSnapshot
from .subscriptions import Subscription, SubscriptionState
from .time import require_aware_datetime


class PlanChangeTiming(str, Enum):
    """Whether a requested plan change takes effect now or at renewal."""

    IMMEDIATE = "immediate"
    AT_PERIOD_END = "at_period_end"


# ``ProrationTiming`` reads more naturally for callers interested only in a
# quote, while ``PlanChangeTiming`` documents what is being timed.
ProrationTiming = PlanChangeTiming


@dataclass(frozen=True, slots=True)
class ProrationQuote:
    """The unrounded financial effect of one requested plan change.

    A deferred quote has zero credit and charge because the current paid
    period is left untouched.  ``effective_at`` is then its exclusive end.
    """

    old_plan: PlanPriceSnapshot
    new_plan: PlanPriceSnapshot
    period_start: datetime
    period_end: datetime
    changed_at: datetime
    effective_at: datetime
    timing: PlanChangeTiming
    credit: ExactAmount
    charge: ExactAmount

    def __post_init__(self) -> None:
        if not isinstance(self.old_plan, PlanPriceSnapshot) or not isinstance(
            self.new_plan, PlanPriceSnapshot
        ):
            raise TypeError("old_plan and new_plan must be PlanPriceSnapshot instances")
        for name in ("period_start", "period_end", "changed_at", "effective_at"):
            require_aware_datetime(getattr(self, name), name=name)
        if self.period_end <= self.period_start:
            raise InvalidProrationError("period end must be after period start")
        if not isinstance(self.timing, PlanChangeTiming):
            raise TypeError("timing must be a PlanChangeTiming")
        if not isinstance(self.credit, ExactAmount) or not isinstance(self.charge, ExactAmount):
            raise TypeError("credit and charge must be ExactAmount instances")
        if self.credit.currency != self.old_plan.price.currency:
            raise CurrencyMismatchError("credit currency must match the old plan")
        if self.charge.currency != self.new_plan.price.currency:
            raise CurrencyMismatchError("charge currency must match the new plan")

    @property
    def net(self) -> ExactAmount:
        """The exact (unrounded) charge, negative for a customer credit."""
        return self.credit + self.charge

    @property
    def is_deferred(self) -> bool:
        return self.timing is PlanChangeTiming.AT_PERIOD_END

    @property
    def is_immediate(self) -> bool:
        return self.timing is PlanChangeTiming.IMMEDIATE


def quote_proration(
    subscription: Subscription,
    new_plan: Plan | PlanPriceSnapshot,
    changed_at: datetime,
    *,
    timing: PlanChangeTiming = PlanChangeTiming.IMMEDIATE,
) -> ProrationQuote:
    """Quote an immediate or renewal-time change without rounding.

    A request at the current period's exact end is always deferred; that
    instant belongs to the following period.  A cross-cadence request is also
    deferred, preserving the existing cycle anchor rather than inventing a
    mid-cycle cadence boundary.
    """
    if not isinstance(subscription, Subscription):
        raise TypeError("subscription must be a Subscription")
    snapshot = _snapshot(new_plan)
    require_aware_datetime(changed_at, name="changed_at")
    if not isinstance(timing, PlanChangeTiming):
        raise TypeError("timing must be a PlanChangeTiming")
    if subscription.state not in {
        SubscriptionState.ACTIVE,
        SubscriptionState.PAST_DUE,
        SubscriptionState.CANCEL_SCHEDULED,
    } or subscription.current_period is None:
        raise InvalidProrationError("plan changes require a paid subscription")

    period = subscription.current_period
    if changed_at < period.start or changed_at > period.end:
        raise InvalidProrationError("changed_at must be in the current period or at its end")
    if snapshot.price.currency != subscription.plan.price.currency:
        raise CurrencyMismatchError("plan changes require matching currencies")

    # Exact period-end requests and cadence changes must wait for renewal.
    deferred = (
        timing is PlanChangeTiming.AT_PERIOD_END
        or changed_at == period.end
        or snapshot.cadence != subscription.plan.cadence
    )
    if deferred:
        return _deferred_quote(subscription, snapshot, changed_at)

    remaining = _remaining_fraction(period.start, period.end, changed_at)
    credit = subscription.plan.price.as_exact() * -remaining
    charge = snapshot.price.as_exact() * remaining
    return ProrationQuote(
        old_plan=subscription.plan,
        new_plan=snapshot,
        period_start=period.start,
        period_end=period.end,
        changed_at=changed_at,
        effective_at=changed_at,
        timing=PlanChangeTiming.IMMEDIATE,
        credit=credit,
        charge=charge,
    )


def quote_plan_change(
    subscription: Subscription,
    new_plan: Plan | PlanPriceSnapshot,
    changed_at: datetime,
    *,
    timing: PlanChangeTiming = PlanChangeTiming.IMMEDIATE,
) -> ProrationQuote:
    """Alias for :func:`quote_proration` with plan-change terminology."""
    return quote_proration(subscription, new_plan, changed_at, timing=timing)


def _snapshot(plan: Plan | PlanPriceSnapshot) -> PlanPriceSnapshot:
    if isinstance(plan, PlanPriceSnapshot):
        return plan
    if isinstance(plan, Plan):
        return plan.snapshot()
    raise TypeError("new_plan must be a Plan or PlanPriceSnapshot")


def _deferred_quote(
    subscription: Subscription, new_plan: PlanPriceSnapshot, changed_at: datetime
) -> ProrationQuote:
    period = subscription.current_period
    assert period is not None  # checked by quote_proration
    return ProrationQuote(
        old_plan=subscription.plan,
        new_plan=new_plan,
        period_start=period.start,
        period_end=period.end,
        changed_at=changed_at,
        effective_at=period.end,
        timing=PlanChangeTiming.AT_PERIOD_END,
        credit=ExactAmount(0, subscription.plan.price.currency),
        charge=ExactAmount(0, new_plan.price.currency),
    )


def _remaining_fraction(start: datetime, end: datetime, changed_at: datetime) -> Fraction:
    """Return remaining / total elapsed time without float conversion."""
    total = _microseconds(end.astimezone(timezone.utc) - start.astimezone(timezone.utc))
    remaining = _microseconds(end.astimezone(timezone.utc) - changed_at.astimezone(timezone.utc))
    return Fraction(remaining, total)


def _microseconds(delta: timedelta) -> int:
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
