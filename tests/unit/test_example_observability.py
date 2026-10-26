"""The observability example: problem details, request IDs, deadlines and health checks."""

import logging
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from examples.observability.main import LOG_FORMAT, LOGGING, Database, app, create_app
from fastapi_alcazaba.request_id import RequestIDLogFilter

ROOT = Path(__file__).parents[2]
PROBLEM = "application/problem+json"
GENERATED_ID = re.compile(r"[0-9a-f]{32}")


@pytest.fixture(name="orders_app")
def fixture_orders_app() -> FastAPI:
    return create_app(request_timeout=0.2)  # a deadline the tests can wait for


@pytest.fixture(name="client")
def fixture_client(orders_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(orders_app) as client:
        yield client


def database(app: FastAPI) -> Database:
    db: Database = app.state.db
    return db


class Lines(logging.Handler):
    """The log lines as the example's format writes them."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.addFilter(RequestIDLogFilter())
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture(name="log_lines")
def fixture_log_lines(caplog: pytest.LogCaptureFixture) -> Iterator[list[str]]:
    caplog.set_level(logging.INFO, logger="orders")
    handler = Lines()
    loggers = [logging.getLogger("orders"), logging.getLogger("fastapi_alcazaba")]
    for logger in loggers:
        logger.addHandler(handler)
    yield handler.lines
    for logger in loggers:
        logger.removeHandler(handler)


class TestProblemDetails:
    def test_validation_errors_never_echo_the_values_sent(self, client: TestClient) -> None:
        response = client.post("/orders", json={"product_id": 1, "quantity": "lots of them"})
        assert (response.status_code, response.headers["content-type"]) == (422, PROBLEM)
        assert response.json() == {
            "type": "about:blank",
            "title": "Unprocessable Content",
            "status": 422,
            "instance": "/orders",
            "errors": [
                {
                    "loc": ["body", "quantity"],
                    "msg": "Input should be a valid integer, unable to parse string as an integer",
                    "type": "int_parsing",
                }
            ],
            "request_id": response.headers["x-request-id"],
        }
        assert "lots of them" not in response.text

    def test_out_of_stock_is_a_problem_with_an_extension(
        self,
        client: TestClient,
        orders_app: FastAPI,
    ) -> None:
        response = client.post("/orders", json={"product_id": 2, "quantity": 5})
        assert (response.status_code, response.headers["content-type"]) == (409, PROBLEM)
        assert response.json() == {
            "type": "https://example.com/problems/out-of-stock",
            "title": "Out of stock",
            "status": 409,
            "detail": "Only 3 left",
            "instance": "/orders",
            "available": 3,
            "request_id": response.headers["x-request-id"],
        }
        assert database(orders_app).stock[2] == 3

    def test_an_order(self, client: TestClient, orders_app: FastAPI) -> None:
        response = client.post("/orders", json={"product_id": 2, "quantity": 3})
        assert (response.status_code, response.json()) == (
            201,
            {"product_id": 2, "quantity": 3, "id": 31},
        )
        assert database(orders_app).stock[2] == 0

    def test_a_4xx_keeps_its_detail(self, client: TestClient) -> None:
        response = client.post("/orders", json={"product_id": 9, "quantity": 1})
        assert (response.status_code, response.headers["content-type"]) == (404, PROBLEM)
        assert response.json()["detail"] == "No such product"
        assert client.get("/nowhere").json()["title"] == "Not Found"

    def test_a_500_tells_nothing_but_the_request_id(self, orders_app: FastAPI) -> None:
        with TestClient(orders_app, raise_server_exceptions=False) as client:
            response = client.get("/orders/3/invoice")
        assert (response.status_code, response.headers["content-type"]) == (500, PROBLEM)
        assert response.json() == {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "instance": "/orders/3/invoice",
            "request_id": response.headers["x-request-id"],
        }
        assert "pdf-renderer" not in response.text

    def test_the_server_still_gets_the_exception(self, client: TestClient) -> None:
        with pytest.raises(ConnectionError, match="pdf-renderer"):
            client.get("/orders/3/invoice")  # what uvicorn logs, with its traceback


class TestRequestIDs:
    def test_every_response_has_a_new_one(self, client: TestClient) -> None:
        first, second = client.get("/health/live"), client.get("/health/live")
        assert GENERATED_ID.fullmatch(first.headers["x-request-id"])
        assert first.headers["x-request-id"] != second.headers["x-request-id"]

    def test_a_safe_incoming_id_is_kept(self, client: TestClient) -> None:
        response = client.get("/nowhere", headers={"X-Request-ID": "lb-7f3a.42"})
        assert response.headers["x-request-id"] == "lb-7f3a.42"
        assert response.json()["request_id"] == "lb-7f3a.42"

    def test_an_unsafe_incoming_id_is_replaced(self, client: TestClient) -> None:
        response = client.get("/health/live", headers={"X-Request-ID": "forged line"})
        assert GENERATED_ID.fullmatch(response.headers["x-request-id"])

    def test_log_lines_carry_it(self, client: TestClient, log_lines: list[str]) -> None:
        response = client.post("/orders", json={"product_id": 1, "quantity": 2})
        [line] = log_lines
        assert line.endswith(f"INFO [{response.headers['x-request-id']}] orders: Order 31 placed")

    def test_outside_requests_lines_have_a_dash(self, log_lines: list[str]) -> None:
        logging.getLogger("orders").info("Starting")
        [line] = log_lines
        assert line.endswith(" INFO [-] orders: Starting")

    def test_every_handler_of_the_logging_config_has_the_filter(self) -> None:
        filter_class = f"{RequestIDLogFilter.__module__}.{RequestIDLogFilter.__qualname__}"
        assert LOGGING["filters"] == {"request_id": {"()": filter_class}}
        formats = {name: f.get("format") or f["fmt"] for name, f in LOGGING["formatters"].items()}
        for handler in LOGGING["handlers"].values():
            assert handler["filters"] == ["request_id"]
            assert "[%(request_id)s]" in formats[handler["formatter"]]

    def test_uvicorn_can_use_the_logging_config(self) -> None:
        # In a process of its own: dictConfig replaces the logging of the whole process.
        script = "\n".join(
            [
                "import logging.config",
                "from examples.observability.main import LOGGING",
                "logging.config.dictConfig(LOGGING)",
                "logging.getLogger('uvicorn.error').info('Application startup complete.')",
                "logging.getLogger('uvicorn.access').info(",
                "    '%s - \"%s %s HTTP/%s\" %d', '127.0.0.1:5000', 'GET', '/', '1.1', 200",
                ")",
            ]
        )
        result = subprocess.run(  # noqa: S603  # this interpreter, a script of ours
            [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
        )
        lines = result.stderr.splitlines()
        assert lines[0].endswith(" INFO [-] uvicorn.error: Application startup complete.")
        assert lines[1].endswith(' INFO [-] "GET / HTTP/1.1" 200 OK')


class TestDeadlines:
    def test_a_slow_request_gets_a_504_with_its_id(
        self,
        client: TestClient,
        orders_app: FastAPI,
        log_lines: list[str],
    ) -> None:
        started = time.monotonic()
        response = client.get("/reports/sales")
        assert time.monotonic() - started < 2
        request_id = response.headers["x-request-id"]  # RequestIDMiddleware is outside
        assert response.headers["content-type"] == "application/problem+json"
        assert (response.status_code, response.json()) == (
            504,
            {
                "type": "about:blank",
                "title": "Gateway Timeout",
                "status": 504,
                "detail": "Request timed out",
                "instance": "/reports/sales",
                "request_id": request_id,
            },
        )
        assert log_lines[-1].endswith(
            f"WARNING [{request_id}] fastapi_alcazaba.timeouts: "
            "GET /reports/sales timed out after 0.2s: answered 504"
        )
        assert database(orders_app).in_use == 0  # the connection went back to the pool

    def test_the_export_streams_past_the_deadline(
        self,
        client: TestClient,
        orders_app: FastAPI,
    ) -> None:
        database(orders_app).row_seconds = 0.02  # 30 rows: three times the deadline
        started = time.monotonic()
        response = client.get("/orders/export")
        assert time.monotonic() - started > 0.2
        assert (response.status_code, response.headers["content-type"]) == (
            200,
            "text/csv; charset=utf-8",
        )
        lines = response.text.splitlines()
        assert (lines[0], lines[-1], len(lines)) == ("id,product_id,quantity", "30,1,1", 31)


class TestHealth:
    def test_ready_checks_the_database_and_the_cache(self, client: TestClient) -> None:
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert (body["status"], set(body["checks"])) == ("ok", {"database", "cache"})
        assert response.headers["cache-control"] == "no-store"

    def test_without_the_database_it_isnt_ready_but_still_alive(
        self,
        client: TestClient,
        orders_app: FastAPI,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        database(orders_app).up = False
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "fail"
        assert response.json()["checks"]["database"]["status"] == "fail"
        assert response.json()["checks"]["cache"]["status"] == "ok"
        assert "db.internal" not in response.text  # logged, never returned
        assert "db.internal" in caplog.text
        assert client.get("/health/live").json() == {"status": "ok"}

    def test_the_default_app(self) -> None:
        with TestClient(app) as client:
            assert client.get("/health/ready").json()["status"] == "ok"
