"""Value objects exchanged between Ledger and a payment adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .errors import InvalidPaymentError


class PaymentOutcome(str, Enum):
    """The adapter's classification of one idempotent collection attempt."""

    SUCCEEDED = "succeeded"
    RETRIABLE_FAILURE = "retriable_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class PaymentAttempt:
    """An adapter-reported outcome for one numbered invoice attempt."""

    invoice_id: str
    attempt_number: int
    idempotency_key: str
    outcome: PaymentOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.invoice_id, str) or not self.invoice_id:
            raise InvalidPaymentError("invoice_id must be a non-empty string")
        if not isinstance(self.attempt_number, int) or isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise InvalidPaymentError("attempt_number must be a positive integer")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise InvalidPaymentError("idempotency_key must be a non-empty string")
        if not isinstance(self.outcome, PaymentOutcome):
            raise InvalidPaymentError("outcome must be a PaymentOutcome")


@runtime_checkable
class PaymentAdapter(Protocol):
    """The application boundary for submitting an idempotent attempt."""

    def collect(self, *, invoice_id: str, attempt_number: int, idempotency_key: str) -> PaymentAttempt:
        """Submit one attempt and return its classified outcome."""


# Kept as a readable spelling for applications which call these results rather
# than outcomes.  Both names denote the same closed enum.
PaymentResult = PaymentOutcome
