"""Strict time primitives used by local-calendar billing rules.

Ledger represents an instant with an aware datetime in a named IANA timezone.
In particular, a fixed-offset ``tzinfo`` is not sufficient for a calendar
recurrence because it has no DST transition rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from .errors import InvalidTimeError


@runtime_checkable
class Clock(Protocol):
    """Application-supplied source of the current instant."""

    def now(self) -> datetime:
        """Return an aware datetime in a named IANA timezone."""


def require_zoneinfo(zone: object, *, name: str = "zone") -> ZoneInfo:
    """Return ``zone`` when it is a named IANA ``ZoneInfo`` instance."""
    if not isinstance(zone, ZoneInfo):
        raise InvalidTimeError(f"{name} must be an IANA zoneinfo.ZoneInfo")
    return zone


def require_aware_datetime(value: object, *, name: str = "datetime") -> datetime:
    """Validate a Ledger instant.

    ``datetime.timezone`` and arbitrary ``tzinfo`` instances are deliberately
    rejected: only ``ZoneInfo`` retains the IANA rules needed for recurrence.
    """
    if not isinstance(value, datetime):
        raise InvalidTimeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTimeError(f"{name} must be timezone-aware")
    require_zoneinfo(value.tzinfo, name=f"{name}.tzinfo")
    return value


def _valid_local_candidates(local: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Return candidates whose local fields survive a UTC round trip."""
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold)
        round_tripped = candidate.astimezone(UTC).astimezone(zone)
        if round_tripped.replace(tzinfo=None) == local and round_tripped.fold == fold:
            candidates.append(candidate)
    return tuple(candidates)


def resolve_local_datetime(local: datetime, zone: ZoneInfo) -> datetime:
    """Resolve local wall time under Ledger's deterministic DST policy.

    ``local`` must be naive.  Ambiguous wall times select the earlier instant
    (``fold=0``).  A nonexistent wall time is moved forward to the first valid
    local minute.  IANA transitions are minute-aligned for the zones Ledger
    supports, and preserving the first valid minute makes the policy explicit
    rather than relying on the platform's imaginary-time behaviour.
    """
    if not isinstance(local, datetime):
        raise InvalidTimeError("local must be a datetime")
    if local.tzinfo is not None:
        raise InvalidTimeError("local must be a naive local datetime")
    zone = require_zoneinfo(zone)

    candidates = _valid_local_candidates(local, zone)
    if candidates:
        return candidates[0]  # fold=0 is always considered first.

    # A DST gap is normally one hour, but use a bounded day-long search so the
    # code also handles the occasional non-hour political timezone change.
    probe = local.replace(second=0, microsecond=0)
    if probe < local:
        probe += timedelta(minutes=1)
    for _ in range(24 * 60 + 1):
        candidates = _valid_local_candidates(probe, zone)
        if candidates:
            return candidates[0]
        probe += timedelta(minutes=1)
    raise InvalidTimeError("could not resolve local datetime in billing zone")


# Short aliases make validation convenient at public-domain boundaries.
validate_zoneinfo = require_zoneinfo
validate_aware_datetime = require_aware_datetime
