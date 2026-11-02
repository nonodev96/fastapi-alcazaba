"""Usage records: one for each charge, sent to the sinks, which is what billing adds up."""

import dataclasses
import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from fastapi_alcazaba.core.logs import outputs

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """A charge, as the sinks get it; `to_dict()` and `to_json()` give its JSON form.

    :param id: random, unique: billing counts each record once, should a sink deliver it twice.
    :param time: when it was charged (UTC).
    :param subject: who used it: a user's, an API key's or a tenant's ID.
    :param meter: what was used, such as "requests" or "tokens".
    :param amount: how much, in the meter's units.
    :param period: the period it was charged to, as its counter keeps it: `2026-11` (a month),
        `2026-11-01` (a day) or `2026-11-01T10` (an hour), in UTC.
    :param request_id: the request's ID (`RequestIDMiddleware`).
    :param trace_id: the OpenTelemetry trace it belongs to.
    :param method: the request's HTTP method.
    :param route: the template of the route that matched, e.g. "/answers/{model}".
    """

    id: str
    time: datetime
    subject: str
    meter: str
    amount: int
    period: str
    request_id: str | None = None
    trace_id: str | None = None
    method: str | None = None
    route: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {item.name: getattr(self, item.name) for item in dataclasses.fields(self)}
        data["time"] = self.time.isoformat()
        return data

    def to_json(self) -> str:
        """One line of JSON, ASCII only: nothing in it can split a log line."""
        return json.dumps(self.to_dict(), separators=(",", ":"))


class UsageSink(Protocol):
    """Where usage records go. `write` gets each one when it's charged, within the
    FastAPIAccounting's `timeout`: queue it if writing takes longer. What it raises is logged,
    with the record."""

    async def write(self, record: UsageRecord) -> None: ...


class LoggingUsageSink:
    """Writes each record as one line of JSON to the logger `log` (a name or a Logger), at
    `level`, for the log pipeline to keep and add up; handlers that format their own JSON find
    the record in the log record's `usage` attribute too. When the logger outputs nothing at that
    level (no handler, or a higher level), it says so once, as a warning."""

    def __init__(
        self,
        log: str | logging.Logger = "fastapi_alcazaba.accounting.usage",
        level: int = logging.INFO,
    ) -> None:
        self.logger = log if isinstance(log, logging.Logger) else logging.getLogger(log)
        self.level = level
        self._warned = False

    async def write(self, record: UsageRecord) -> None:
        if not self._warned and not outputs(self.logger, self.level):
            self._warned = True
            logger.warning(
                "Usage records are being lost: the logger %r outputs nothing at level %s. "
                "Give it a handler, or the root logger a level that lets them through",
                self.logger.name,
                logging.getLevelName(self.level),
            )
        self.logger.log(self.level, "%s", record.to_json(), extra={"usage": record.to_dict()})


class MemoryUsageSink:
    """Keeps the last `maxlen` records in `records`: for tests, and to look at them while
    developing."""

    def __init__(self, maxlen: int | None = 1000) -> None:
        self.records: deque[UsageRecord] = deque(maxlen=maxlen)

    async def write(self, record: UsageRecord) -> None:
        self.records.append(record)
