"""Structured logs: one line of JSON per record, with the request's ID and trace, for the log
pipeline to search (CloudWatch, Loki, Elasticsearch, a SIEM).

    uvicorn.run("main:app", log_config=logging_config(static={"service": "shop"}))

    logger.info("Order placed", extra={"order_id": 42, "token": token})  # token: "[redacted]"

`JSONFormatter` writes each record's time (RFC 3339, UTC), level, logger, message, the ID of the
request being handled (`RequestIDMiddleware`), its OpenTelemetry trace and span, the exception
when there's one, and the fields given in `extra=`, as the audit trail records details: JSON
values only, the values of keys that name a secret and the strings that look like credentials
redacted, everything bounded. Uvicorn's access log gets the method, the path without its query
(which may carry tokens), the status and the client.
"""

import json
import logging
import traceback
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi_alcazaba.audit.trail import MAX_STRING, redacted
from fastapi_alcazaba.observability.request_id import request_id

try:
    from opentelemetry import trace as otel_trace
except ImportError:  # pragma: no cover  # the otel extra isn't installed
    otel_trace = None  # type: ignore[assignment]

#: What every LogRecord has: the rest of its attributes came in `extra=`.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}
#: Frames of a traceback kept, the last ones.
_MAX_FRAMES = 30


class JSONFormatter(logging.Formatter):
    """Formats each record as one line of JSON, ASCII only: nothing in it splits a line, a
    message with line breaks included.

    :param static: fields every line has, such as {"service": "shop", "environment": "prod"}.
    :param redact: words that mark secrets in the keys of `extra`, besides the audit trail's
        `SENSITIVE_WORDS`.
    """

    def __init__(
        self,
        *,
        static: Mapping[str, Any] | None = None,
        redact: Collection[str] = (),
    ) -> None:
        super().__init__()
        self.static = dict(static or {})
        if isinstance(redact, str):
            raise TypeError("redact is a list of words, not a single string")
        self.redact = tuple(redact)

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            **self.static,
        }
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            line.update(_access(record.args))
        else:
            line["message"] = _cut(record.getMessage())
        found = request_id()
        if found is not None:
            line["request_id"] = found
        line.update(_trace())
        extra = {key: value for key, value in record.__dict__.items() if key not in _STANDARD}
        if extra:
            line["extra"] = redacted(extra, redact=self.redact)
        if record.exc_info and record.exc_info[0] is not None:
            kind, error, stack = record.exc_info
            line["exception"] = {
                "type": f"{kind.__module__}.{kind.__qualname__}",
                "message": _cut(str(error)),
                "stack": traceback.format_tb(stack, limit=-_MAX_FRAMES) if stack else [],
            }
        return json.dumps(line, default=str, separators=(",", ":"))


def _cut(text: str) -> str:
    return text if len(text) <= MAX_STRING * 8 else f"{text[: MAX_STRING * 8]}…[cut]"


def _access(args: tuple[Any, ...]) -> dict[str, Any]:
    """Uvicorn's access record: (client, method, path with its query, HTTP version, status)."""
    if len(args) != 5:
        return {"message": "access"}
    client, method, path, _, status = args
    plain = str(path).split("?", 1)[0]  # the query may carry tokens, codes, e-mail addresses
    return {
        "message": f"{method} {plain} {status}",
        "http": {"method": method, "path": plain, "status": status, "client": client},
    }


def _trace() -> dict[str, str]:
    if otel_trace is not None:  # pragma: no branch  # the tests have the otel extra
        context = otel_trace.get_current_span().get_span_context()
        if context.is_valid:
            return {"trace_id": f"{context.trace_id:032x}", "span_id": f"{context.span_id:016x}"}
    return {}


def logging_config(
    level: str = "INFO",
    *,
    static: Mapping[str, Any] | None = None,
    redact: Collection[str] = (),
    access_log: bool = True,
) -> dict[str, Any]:
    """A `logging.config.dictConfig` that sends every record, Uvicorn's too, to standard output
    as JSON: for `uvicorn.run(..., log_config=logging_config())`, or `dictConfig()` at start.

    :param level: of the app's loggers and the root.
    :param static: fields every line has ({"service": "shop"}).
    :param redact: more words that mark secrets in `extra`.
    :param access_log: keep Uvicorn's line per request.
    """
    formatter = {"()": JSONFormatter, "static": dict(static or {}), "redact": list(redact)}
    handler = {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stdout"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": formatter},
        "handlers": {"stdout": handler},
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            "uvicorn": {"level": level, "handlers": ["stdout"], "propagate": False},
            "uvicorn.error": {"level": level, "handlers": ["stdout"], "propagate": False},
            "uvicorn.access": {
                "level": "INFO" if access_log else "WARNING",
                "handlers": ["stdout"],
                "propagate": False,
            },
        },
    }
