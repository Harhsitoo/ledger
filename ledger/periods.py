"""Calendar-anchored, half-open billing periods."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

from .errors import InvalidPeriodError, InvalidTimeError
from .time import require_aware_datetime, require_zoneinfo, resolve_local_datetime


class BillingCadence(str, Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


def _require_cadence(cadence: object) -> BillingCadence:
    if not isinstance(cadence, BillingCadence):
        raise InvalidPeriodError("cadence must be a BillingCadence")
    return cadence


@dataclass(frozen=True, slots=True)
class CycleAnchor:
    """The original paid start expressed as local calendar fields.

    These fields never change.  In particular, an anchor with day 31 is still
    day 31 after a February boundary was necessarily clamped to day 28 or 29.
    """

    local_date: date
    local_time: time
    zone: ZoneInfo

    def __post_init__(self) -> None:
        if not isinstance(self.local_date, date) or isinstance(self.local_date, datetime):
            raise InvalidTimeError("local_date must be a date")
        if not isinstance(self.local_time, time):
            raise InvalidTimeError("local_time must be a time")
        if self.local_time.tzinfo is not None:
            raise InvalidTimeError("local_time must be naive")
        require_zoneinfo(self.zone)

    @classmethod
    def from_start(cls, started_at: datetime, zone: ZoneInfo | None = None) -> "CycleAnchor":
        """Establish an anchor from the first paid-period start instant."""
        require_aware_datetime(started_at, name="started_at")
        billing_zone = require_zoneinfo(zone or started_at.tzinfo, name="zone")
        local = started_at.astimezone(billing_zone)
        return cls(local.date(), local.timetz().replace(tzinfo=None), billing_zone)

    @property
    def day(self) -> int:
        return self.local_date.day


@dataclass(frozen=True, slots=True)
class Period:
    """A non-empty half-open ``[start, end)`` interval in one billing zone."""

    start: datetime
    end: datetime
    zone: ZoneInfo

    def __post_init__(self) -> None:
        require_aware_datetime(self.start, name="start")
        require_aware_datetime(self.end, name="end")
        require_zoneinfo(self.zone)
        if self.end <= self.start:
            raise InvalidPeriodError("period end must be after period start")

    def contains(self, instant: datetime) -> bool:
        """Whether ``instant`` belongs to this half-open period."""
        require_aware_datetime(instant, name="instant")
        return self.start <= instant < self.end

    def __contains__(self, instant: object) -> bool:
        return isinstance(instant, datetime) and self.contains(instant)


def cycle_boundary(anchor: CycleAnchor, cadence: BillingCadence, index: int) -> datetime:
    """Return boundary *index*, always calculated from the original anchor.

    This is deliberately not an operation on the preceding boundary: clamping
    only happens for the target month, so an anchor on the 31st returns to the
    31st as soon as the target month has one.
    """
    if not isinstance(anchor, CycleAnchor):
        raise TypeError("anchor must be a CycleAnchor")
    cadence = _require_cadence(cadence)
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise InvalidPeriodError("boundary index must be a non-negative integer")

    if cadence is BillingCadence.MONTHLY:
        month_number = anchor.local_date.month - 1 + index
        year = anchor.local_date.year + month_number // 12
        month = month_number % 12 + 1
    else:
        year = anchor.local_date.year + index
        month = anchor.local_date.month
    day = min(anchor.local_date.day, calendar.monthrange(year, month)[1])
    local = datetime.combine(date(year, month, day), anchor.local_time)
    return resolve_local_datetime(local, anchor.zone)


def period_for_cycle(anchor: CycleAnchor, cadence: BillingCadence, index: int) -> Period:
    """Return paid cycle *index* as a half-open interval."""
    return Period(
        start=cycle_boundary(anchor, cadence, index),
        end=cycle_boundary(anchor, cadence, index + 1),
        zone=anchor.zone,
    )


def next_cycle_boundary(anchor: CycleAnchor, cadence: BillingCadence, index: int) -> datetime:
    """Return the boundary following cycle *index*."""
    return cycle_boundary(anchor, cadence, index + 1)


# Readable aliases for callers that use "advance" terminology.
advance_cycle = cycle_boundary
cycle_period = period_for_cycle
