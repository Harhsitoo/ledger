"""Domain-specific exceptions raised by Ledger."""


class LedgerError(Exception):
    """Base class for errors caused by invalid domain operations."""


class InvalidCurrencyError(LedgerError, ValueError):
    """A currency code or minor-unit exponent is invalid."""


class InvalidMoneyAmountError(LedgerError, TypeError):
    """A public monetary amount is not an integer number of minor units."""


class CurrencyMismatchError(LedgerError, ValueError):
    """An operation was attempted between different currencies."""


class AllocationError(LedgerError, ValueError):
    """An allocation request cannot produce a valid deterministic split."""


class EmptyAllocationError(AllocationError):
    """An allocation was requested with no recipients."""


class InvalidWeightError(AllocationError):
    """An allocation weight is not a positive integer."""


class InvalidTimeError(LedgerError, ValueError):
    """A datetime or billing timezone cannot be used by Ledger."""


class InvalidPeriodError(LedgerError, ValueError):
    """A billing period does not describe a non-empty valid interval."""
