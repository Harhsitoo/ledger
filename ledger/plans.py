"""Immutable plan prices used by subscriptions and historical billing records."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidPlanError
from .money import Money
from .periods import BillingCadence


def _require_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidPlanError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class Plan:
    """The current, editable commercial definition of a plan.

    Never store this object on a historical billing record.  Use
    :meth:`snapshot` instead, so a replacement plan price cannot rewrite it.
    """

    id: str
    name: str
    price: Money
    cadence: BillingCadence

    def __post_init__(self) -> None:
        _require_text(self.id, name="plan id")
        _require_text(self.name, name="plan name")
        if not isinstance(self.price, Money):
            raise InvalidPlanError("plan price must be Money")
        if not isinstance(self.cadence, BillingCadence):
            raise InvalidPlanError("plan cadence must be a BillingCadence")

    def snapshot(self) -> "PlanPriceSnapshot":
        return PlanPriceSnapshot(self.id, self.name, self.price, self.cadence)


@dataclass(frozen=True, slots=True)
class PlanPriceSnapshot:
    """An immutable copy of every price input needed to bill a plan."""

    plan_id: str
    plan_name: str
    price: Money
    cadence: BillingCadence

    def __post_init__(self) -> None:
        _require_text(self.plan_id, name="plan_id")
        _require_text(self.plan_name, name="plan_name")
        if not isinstance(self.price, Money):
            raise InvalidPlanError("snapshot price must be Money")
        if not isinstance(self.cadence, BillingCadence):
            raise InvalidPlanError("snapshot cadence must be a BillingCadence")

    @classmethod
    def from_plan(cls, plan: Plan) -> "PlanPriceSnapshot":
        if not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan")
        return cls(plan.id, plan.name, plan.price, plan.cadence)
