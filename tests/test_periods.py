from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ledger.errors import InvalidPeriodError, InvalidTimeError
from ledger.periods import BillingCadence, CycleAnchor, Period, cycle_boundary, period_for_cycle


UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def anchor(year: int, month: int, day: int, *, zone: ZoneInfo = UTC) -> CycleAnchor:
    return CycleAnchor.from_start(datetime(year, month, day, 10, 15, tzinfo=zone))


@pytest.mark.parametrize("day", [28, 29, 30, 31])
@pytest.mark.parametrize("year", [2024, 2026])  # leap and non-leap years
def test_monthly_anchor_days_do_not_drift_over_a_full_year(day, year):
    start_month = 1
    start_day = day
    cycle_anchor = anchor(year, start_month, start_day)
    boundaries = [cycle_boundary(cycle_anchor, BillingCadence.MONTHLY, index) for index in range(13)]

    assert [boundary.month for boundary in boundaries] == [*range(1, 13), 1]
    assert boundaries[0].day == day
    assert boundaries[2].day == day  # March restores the original anchor day.
    assert boundaries[4].day == day  # May also restores it after April.
    assert all(boundary.hour == 10 and boundary.minute == 15 for boundary in boundaries)

    february = boundaries[1]
    expected_february_day = min(day, 29 if year % 4 == 0 else 28)
    assert february.day == expected_february_day


def test_monthly_anchor_29_uses_february_29_in_a_leap_year_and_restores_afterward():
    cycle_anchor = anchor(2024, 1, 29)
    assert cycle_boundary(cycle_anchor, BillingCadence.MONTHLY, 1).date().isoformat() == "2024-02-29"
    assert cycle_boundary(cycle_anchor, BillingCadence.MONTHLY, 2).date().isoformat() == "2024-03-29"


def test_monthly_anchor_31_clamps_only_target_month_and_never_drifts():
    cycle_anchor = anchor(2026, 1, 31)
    assert [cycle_boundary(cycle_anchor, BillingCadence.MONTHLY, index).date().isoformat() for index in range(4)] == [
        "2026-01-31",
        "2026-02-28",
        "2026-03-31",
        "2026-04-30",
    ]


def test_yearly_february_29_returns_in_a_later_leap_year():
    cycle_anchor = anchor(2024, 2, 29)
    assert [cycle_boundary(cycle_anchor, BillingCadence.YEARLY, index).date().isoformat() for index in range(5)] == [
        "2024-02-29",
        "2025-02-28",
        "2026-02-28",
        "2027-02-28",
        "2028-02-29",
    ]


def test_consecutive_periods_are_adjacent_without_overlap_or_gap():
    cycle_anchor = anchor(2026, 1, 31)
    periods = [period_for_cycle(cycle_anchor, BillingCadence.MONTHLY, index) for index in range(12)]
    for current, following in zip(periods, periods[1:]):
        assert current.end == following.start
        assert current.end not in current
        assert current.end in following


def test_period_is_half_open_at_its_end_boundary():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 2, 1, tzinfo=UTC)
    period = Period(start, end, UTC)
    assert start in period
    assert end not in period


def test_dst_cycle_boundaries_keep_wall_time_and_apply_resolution_policy():
    spring = CycleAnchor.from_start(datetime(2026, 1, 8, 2, 30, tzinfo=NY))
    spring_boundary = cycle_boundary(spring, BillingCadence.MONTHLY, 2)
    assert spring_boundary == datetime(2026, 3, 8, 3, 0, tzinfo=NY)

    fall = CycleAnchor.from_start(datetime(2026, 8, 1, 1, 30, tzinfo=NY))
    fall_boundary = cycle_boundary(fall, BillingCadence.MONTHLY, 3)
    assert fall_boundary.fold == 0
    assert fall_boundary.utcoffset().total_seconds() == -4 * 60 * 60


def test_period_rejects_naive_and_empty_intervals():
    aware = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(InvalidTimeError):
        Period(datetime(2026, 1, 1), aware, UTC)
    with pytest.raises(InvalidPeriodError):
        Period(aware, aware, UTC)
