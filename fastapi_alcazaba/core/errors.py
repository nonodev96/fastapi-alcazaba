"""Warnings and exceptions of Alcazaba."""


class UnsignedEntriesError(ValueError):
    """A coder whose decoding can run code (PickleCoder) without a signing or encryption key:
    whoever can write to the backend could run code in the app."""


class UnstableCacheKeyWarning(UserWarning):
    """An argument in the cache key has a repr() that changes with every call (it shows a memory
    address), so the entry is never found again: leave it out with `exclude_params`."""


class BackendUnavailable(Exception):
    """The backend failed, timed out or its circuit breaker is open. The decorator treats it as
    "no cache" and runs the function."""


class CircuitOpen(BackendUnavailable):
    """The circuit breaker is open: the backend isn't called for a while."""


class ValueTooLarge(ValueError):
    """A value larger than the backend takes (`Backend.max_value_size`): refused before it's
    sent. The backend is fine, so it doesn't count against its circuit breaker."""
