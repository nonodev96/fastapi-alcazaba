"""Warnings and exceptions of Alcazaba."""


class UnsignedEntriesWarning(UserWarning):
    """A coder whose decoding can run code (PickleCoder) is used without a signing key."""


class UnstableCacheKeyWarning(UserWarning):
    """An argument in the cache key has a repr() that changes with every call (it shows a memory
    address), so the entry is never found again: leave it out with `exclude_params`."""


class BackendUnavailable(Exception):
    """The backend failed, timed out or its circuit breaker is open. The decorator treats it as
    "no cache" and runs the function."""


class CircuitOpen(BackendUnavailable):
    """The circuit breaker is open: the backend isn't called for a while."""
