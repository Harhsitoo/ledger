from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ledger.errors import InvalidTimeError
from ledger.time import require_aware_datetime, require_zoneinfo, resolve_local_datetime


NY = ZoneInfo("America/New_York")


def test_named_iana_zone_and_aware_datetime_are_accepted():
    instant = datetime(2026, 1, 1, 12, tzinfo=NY)
    assert require_zoneinfo(NY) is NY
    assert require_aware_datetime(instant) is instant


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 1, 1, 12), datetime(2026, 1, 1, 12, tzinfo=timezone.utc)],
)
def test_naive_and_offset_only_datetimes_are_rejected(value):
    with pytest.raises(InvalidTimeError):
        require_aware_datetime(value)


def test_offset_only_billing_zone_is_rejected():
    with pytest.raises(InvalidTimeError):
        require_zoneinfo(timezone.utc)


def test_spring_forward_nonexistent_local_time_moves_to_first_valid_time():
    resolved = resolve_local_datetime(datetime(2026, 3, 8, 2, 30), NY)
    assert resolved == datetime(2026, 3, 8, 3, 0, tzinfo=NY)
    assert resolved.fold == 0


def test_fall_back_ambiguous_local_time_uses_earlier_occurrence():
    resolved = resolve_local_datetime(datetime(2026, 11, 1, 1, 30), NY)
    assert resolved.fold == 0
    assert resolved.utcoffset().total_seconds() == -4 * 60 * 60
