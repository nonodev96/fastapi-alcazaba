"""JSONFormatter and logging_config: one line of JSON per record, with the request's ID and trace,
secrets redacted, and Uvicorn's access log without the query."""

import json
import logging
import logging.config
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from fastapi_alcazaba.observability import JSONFormatter, RequestIDMiddleware, logging_config

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbmEifQ.c2lnbmF0dXJl"


def line(logged: logging.LogRecord, **options: Any) -> dict[str, Any]:
    formatted = JSONFormatter(**options).format(logged)
    assert "\n" not in formatted
    found: dict[str, Any] = json.loads(formatted)
    return found


def record(message: str, *args: Any, **extra: Any) -> logging.LogRecord:
    made = logging.LogRecord("shop.orders", logging.INFO, "orders.py", 7, message, args, None)
    made.__dict__.update(extra)
    return made


def test_a_line() -> None:
    found = line(record("Order %s placed", 42), static={"service": "shop"})
    assert found["message"] == "Order 42 placed"
    assert (found["level"], found["logger"], found["service"]) == ("INFO", "shop.orders", "shop")
    assert found["time"].endswith("+00:00")
    assert "extra" not in found
    assert "request_id" not in found  # outside a request


def test_extra_fields_with_secrets_redacted() -> None:
    secrets = {"token": "abc", "auth": f"Bearer {JWT}"}
    user = {"password": "x", "name": "ana"}
    found = line(record("Order placed", order_id=42, user=user, **secrets))
    assert found["extra"] == {
        "order_id": 42,
        "token": "[redacted]",
        "user": {"password": "[redacted]", "name": "ana"},
        "auth": "[redacted]",
    }
    found = line(record("Order placed", iban="ES91"), redact=["iban"])
    assert found["extra"] == {"iban": "[redacted]"}


def test_values_that_arent_json() -> None:
    found = line(record("Order placed", blob=b"\x00\x01", thing=object(), big=2**60))
    assert found["extra"]["blob"] == "<2 bytes>"
    assert found["extra"]["thing"] == "<object>"
    assert found["extra"]["big"] == str(2**60)


def test_line_breaks_cant_forge_lines() -> None:
    formatted = JSONFormatter().format(record('ok\n{"level": "CRITICAL"}'))
    assert formatted.count("\n") == 0
    assert json.loads(formatted)["level"] == "INFO"


def test_an_exception() -> None:
    made: logging.LogRecord | None = None
    try:
        raise ValueError("bad order")
    except ValueError:
        made = logging.LogRecord(
            "shop", logging.ERROR, "orders.py", 9, "Failed", None, sys.exc_info()
        )
    assert made is not None
    found = line(made)
    assert found["exception"]["type"] == "builtins.ValueError"
    assert found["exception"]["message"] == "bad order"
    assert any("test_an_exception" in frame for frame in found["exception"]["stack"])


class Lines(logging.Handler):
    """Formats each record when it's logged, as a handler writing it out would."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JSONFormatter())
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:  # pylint: disable=redefined-outer-name
        self.lines.append(json.loads(self.format(record)))


def test_the_request_id_and_the_trace() -> None:
    app = FastAPI()
    logger = logging.getLogger("shop.test_logs")
    handler = Lines()
    logger.addHandler(handler)
    tracer = TracerProvider().get_tracer("test")

    @app.get("/")
    async def index() -> None:
        with tracer.start_as_current_span("work"):
            logger.warning("in a request")

    app.add_middleware(RequestIDMiddleware)
    try:
        response = TestClient(app).get("/", headers={"X-Request-ID": "req-123"})
    finally:
        logger.removeHandler(handler)
    assert response.headers["x-request-id"] == "req-123"
    assert len(handler.lines) == 1
    found = handler.lines[0]
    assert found["request_id"] == "req-123"
    assert len(found["trace_id"]) == 32
    assert len(found["span_id"]) == 16


def test_uvicorns_access_log_without_the_query() -> None:
    access = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "h11_impl.py",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/reset?token=s3cret", "1.1", 200),
        None,
    )
    found = line(access)
    assert found["message"] == "GET /reset 200"
    http = {"method": "GET", "path": "/reset", "status": 200, "client": "127.0.0.1:5000"}
    assert found["http"] == http
    assert "s3cret" not in json.dumps(found)


def test_a_long_message_is_cut() -> None:
    assert line(record("x" * 10_000))["message"].endswith("…[cut]")


@pytest.fixture(name="configured")
def fixture_configured() -> Iterator[None]:
    """The loggers dictConfig changes, as they were before, afterwards: the other tests capture
    their records."""
    loggers = [
        logging.getLogger(name) for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    saved = [(each.level, list(each.handlers), each.propagate, each.disabled) for each in loggers]
    yield
    for each, (level, handlers, propagate, disabled) in zip(loggers, saved, strict=True):
        for handler in set(each.handlers) - set(handlers):
            handler.close()
        each.handlers[:] = handlers
        each.setLevel(level)
        each.propagate = propagate
        each.disabled = disabled


@pytest.mark.usefixtures("configured")
def test_logging_config(capsys: pytest.CaptureFixture[str]) -> None:
    config = logging_config("INFO", static={"service": "shop"}, access_log=False)
    assert config["loggers"]["uvicorn.access"]["level"] == "WARNING"
    logging.config.dictConfig(config)
    logging.getLogger("shop.configured").info("configured", extra={"password": "x"})
    logging.getLogger("uvicorn.error").info("started")
    lines = [json.loads(text) for text in capsys.readouterr().out.splitlines()]
    assert [(entry["logger"], entry["message"]) for entry in lines] == [
        ("shop.configured", "configured"),
        ("uvicorn.error", "started"),
    ]
    assert lines[0]["extra"] == {"password": "[redacted]"}
    assert lines[0]["service"] == "shop"


def test_redact_is_a_list() -> None:
    with pytest.raises(TypeError, match="list of words"):
        JSONFormatter(redact="iban")
