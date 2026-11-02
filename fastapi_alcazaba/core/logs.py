"""What the tools that write records to a logger share (the audit trail, the usage records)."""

import logging


def outputs(log: logging.Logger, level: int) -> bool:
    """Whether a record of `level` on `log` reaches a handler that writes it."""
    if not log.isEnabledFor(level):
        return False
    current: logging.Logger | None = log
    while current is not None:
        for handler in current.handlers:
            if not isinstance(handler, logging.NullHandler) and level >= handler.level:
                return True
        if not current.propagate:
            return False
        current = current.parent
    last_resort = logging.lastResort  # what logging uses when there's no handler at all
    return last_resort is not None and level >= last_resort.level
