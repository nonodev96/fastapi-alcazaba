"""FastAPIAudit: events of endpoints and code, redaction, sinks, signatures and their checks."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import dataclasses
import enum
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from starlette.requests import HTTPConnection
from starlette.status import WS_1008_POLICY_VIOLATION, WS_1011_INTERNAL_ERROR
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import FastAPIAudit
from fastapi_alcazaba import audit as audit_module
from fastapi_alcazaba.audit import (
    MAX_ITEMS,
    MAX_STRING,
    REDACTED,
    AuditEntry,
    AuditEvent,
    LoggingAuditSink,
    MemoryAuditSink,
    verify_audit_trail,
)
from fastapi_alcazaba.metrics import Metrics
from fastapi_alcazaba.request_id import RequestIDMiddleware

KEY = "k" * 32
OLD_KEY = "o" * 32
ALICE = {"Authorization": "Bearer alice-token"}


@pytest.fixture(name="trail")
def fixture_trail() -> MemoryAuditSink:
    return MemoryAuditSink()


@pytest.fixture(name="audit")
def fixture_audit(trail: MemoryAuditSink) -> FastAPIAudit:
    return FastAPIAudit([trail], signing_key=KEY)


async def current_user(connection: HTTPConnection) -> str:
    if connection.headers.get("authorization") != "Bearer alice-token":
        raise HTTPException(401, "Not authenticated")
    return "alice"


def make_app(audit: FastAPIAudit) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    delete = audit.action("invoice.delete", resource="invoice:{invoice_id}")

    # The audit dependency first: it also records the requests the authentication refuses.
    @app.delete("/invoices/{invoice_id}", status_code=204)
    async def delete_invoice(
        invoice_id: int,
        entry: Annotated[AuditEntry, Depends(delete)],
        user: Annotated[str, Depends(current_user)],
    ) -> None:
        entry.actor = user
        entry.details["amount"] = 1250
        if invoice_id == 403:
            entry.reason = "not the owner"
            raise HTTPException(403, "Forbidden")
        if invoice_id == 404:
            raise HTTPException(404, "Not found")
        if invoice_id == 500:
            raise ValueError("password=hunter2")

    @app.post("/invoices/{invoice_id}/refund")
    async def refund(
        entry: Annotated[AuditEntry, Depends(audit.action("invoice.refund"))],
    ) -> JSONResponse:
        entry.outcome = "denied"  # answered without raising
        return JSONResponse({"detail": "Refunds are closed"}, status_code=403)

    @app.get("/old-invoices")
    async def moved(
        _entry: Annotated[AuditEntry, Depends(audit.action("invoice.list"))],
    ) -> None:
        raise HTTPException(307, headers={"Location": "/invoices"})

    @app.get("/reports/{year}")
    def report(
        entry: Annotated[AuditEntry, Depends(audit.action("report.read"))],
    ) -> RedirectResponse:
        entry.actor = 7
        return RedirectResponse("/reports")

    @app.get("/missing/{name}")
    async def missing(
        _entry: Annotated[AuditEntry, Depends(audit.action("x.read", resource="x:{other}"))],
    ) -> None:
        return None

    return app


@pytest.fixture(name="client")
def fixture_client(audit: FastAPIAudit) -> Iterator[TestClient]:
    with TestClient(make_app(audit), raise_server_exceptions=False) as client:
        yield client


def only(trail: MemoryAuditSink) -> AuditEvent:
    assert len(trail.events) == 1, trail.events
    return trail.events[0]


def test_an_action_that_succeeds(client: TestClient, trail: MemoryAuditSink) -> None:
    response = client.delete("/invoices/7", headers=ALICE)

    assert response.status_code == 204
    event = only(trail)
    assert event.action == "invoice.delete"
    assert event.outcome == "success"
    assert (event.actor, event.resource) == ("alice", "invoice:7")
    assert (event.reason, event.status) == (None, None)
    assert event.details == {"amount": 1250}
    assert event.request_id == response.headers["X-Request-ID"]
    assert (event.method, event.route, event.client) == (
        "DELETE",
        "/invoices/{invoice_id}",
        "testclient",
    )
    assert event.time.tzinfo is UTC


def test_a_request_the_authentication_refuses_is_denied(
    client: TestClient,
    trail: MemoryAuditSink,
) -> None:
    assert client.delete("/invoices/7").status_code == 401

    event = only(trail)
    assert (event.outcome, event.status) == ("denied", 401)
    assert event.reason == "fastapi.exceptions.HTTPException"
    assert (event.actor, event.resource) == (None, "invoice:7")


def test_a_reason_given_by_the_endpoint(client: TestClient, trail: MemoryAuditSink) -> None:
    assert client.delete("/invoices/403", headers=ALICE).status_code == 403

    event = only(trail)
    assert (event.outcome, event.status, event.reason) == ("denied", 403, "not the owner")
    assert event.actor == "alice"


def test_a_failure_records_the_exception_type_not_its_message(
    client: TestClient,
    trail: MemoryAuditSink,
) -> None:
    assert client.delete("/invoices/500", headers=ALICE).status_code == 500

    event = only(trail)
    assert (event.outcome, event.reason, event.status) == ("failure", "ValueError", None)
    assert "hunter2" not in event.to_json()


def test_other_http_errors_are_failures(client: TestClient, trail: MemoryAuditSink) -> None:
    assert client.delete("/invoices/404", headers=ALICE).status_code == 404

    assert (only(trail).outcome, only(trail).status) == ("failure", 404)


def test_an_invalid_request_is_a_failure(client: TestClient, trail: MemoryAuditSink) -> None:
    assert client.delete("/invoices/seven", headers=ALICE).status_code == 422

    event = only(trail)
    assert (event.outcome, event.status) == ("failure", 422)
    assert event.reason == "fastapi.exceptions.RequestValidationError"
    assert event.resource == "invoice:seven"


def test_an_outcome_set_by_an_endpoint_that_answers_without_raising(
    client: TestClient,
    trail: MemoryAuditSink,
) -> None:
    assert client.post("/invoices/7/refund").status_code == 403

    assert only(trail).outcome == "denied"


def test_a_redirection_raised_is_a_success(client: TestClient, trail: MemoryAuditSink) -> None:
    assert client.get("/old-invoices", follow_redirects=False).status_code == 307

    event = only(trail)
    assert (event.outcome, event.status, event.reason) == ("success", 307, None)


def test_a_sync_endpoint(client: TestClient, trail: MemoryAuditSink) -> None:
    client.get("/reports/2026", follow_redirects=False)

    event = only(trail)
    assert (event.action, event.outcome, event.actor) == ("report.read", "success", "7")


def test_a_resource_template_needs_the_route_s_parameters(audit: FastAPIAudit) -> None:
    with TestClient(make_app(audit)) as client, pytest.raises(RuntimeError, match="'other'"):
        client.get("/missing/x")


@pytest.mark.parametrize(
    "template",
    [
        "invoice:{invoice_id.__class__}",
        "invoice:{invoice_id!r}",
        "invoice:{invoice_id:>9}",
        "{0}",
        "{}",
    ],
)
def test_resource_templates_only_take_path_parameters(audit: FastAPIAudit, template: str) -> None:
    with pytest.raises(ValueError, match="invalid resource template"):
        audit.action("invoice.delete", resource=template)


def test_websockets(audit: FastAPIAudit, trail: MemoryAuditSink) -> None:
    app = FastAPI()

    @app.websocket("/chat/{room}")
    async def chat(
        websocket: WebSocket,
        room: str,
        entry: Annotated[AuditEntry, Depends(audit.action("chat.join", resource="room:{room}"))],
    ) -> None:
        if room == "private":
            raise WebSocketException(WS_1008_POLICY_VIOLATION)
        if room == "broken":
            raise WebSocketException(WS_1011_INTERNAL_ERROR)
        entry.actor = "alice"
        await websocket.accept()
        await websocket.close()

    @app.websocket("/feed")
    async def feed(
        websocket: WebSocket,
        _topic: str,  # required, and missing: an invalid request
        _entry: Annotated[AuditEntry, Depends(audit.action("feed.read"))],
    ) -> None:
        await websocket.accept()

    with TestClient(app) as client:
        with client.websocket_connect("/chat/general"):
            pass
        for room in ("private", "broken"):
            with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/chat/{room}"):
                pass
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/feed"):
            pass

    events = list(trail.events)
    assert [(event.resource, event.outcome) for event in events[:3]] == [
        ("room:general", "success"),
        ("room:private", "denied"),
        ("room:broken", "failure"),
    ]
    assert (events[0].method, events[0].route, events[0].actor) == (None, "/chat/{room}", "alice")
    assert (events[3].outcome, events[3].status) == ("failure", None)
    assert events[3].reason == "fastapi.exceptions.WebSocketRequestValidationError"


async def test_record_a_block_of_code(audit: FastAPIAudit, trail: MemoryAuditSink) -> None:
    async with audit.record("backup.restore", actor="cron", details={"file": "b.tar"}) as entry:
        entry.resource = "database:main"
    with pytest.raises(OSError, match="disk full"):
        async with audit.record("backup.create", actor="cron"):
            raise OSError("disk full")

    done, failed = trail.events
    assert (done.action, done.outcome, done.actor, done.resource) == (
        "backup.restore",
        "success",
        "cron",
        "database:main",
    )
    assert done.details == {"file": "b.tar"}
    assert (done.request_id, done.method, done.route, done.client) == (None, None, None, None)
    assert (failed.outcome, failed.reason) == ("failure", "OSError")


async def test_a_cancelled_block_is_recorded(audit: FastAPIAudit, trail: MemoryAuditSink) -> None:
    started = asyncio.Event()

    async def job() -> None:
        async with audit.record("report.build"):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(job())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (only(trail).outcome, only(trail).reason) == ("failure", "cancelled")


async def test_an_event_is_written_even_if_its_writer_is_cancelled_meanwhile() -> None:
    gate = asyncio.Event()
    written: list[AuditEvent] = []

    class Slow:
        async def write(self, event: AuditEvent) -> None:
            await gate.wait()
            written.append(event)

    audit = FastAPIAudit([Slow()])
    task = asyncio.create_task(audit.log("user.delete", actor="alice"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.sleep(0.01)

    assert [event.action for event in written] == ["user.delete"]
    assert not audit._writing  # the task is forgotten once done


async def test_a_dependency_closed_before_it_ends_writes_nothing(
    audit: FastAPIAudit,
    trail: MemoryAuditSink,
) -> None:
    connection = HTTPConnection({"type": "http", "headers": [], "path_params": {}})
    dependency = audit.action("invoice.read")(connection)
    await anext(dependency)
    await dependency.aclose()  # type: ignore[attr-defined]

    assert not trail.events


async def test_log_one_event(audit: FastAPIAudit, trail: MemoryAuditSink) -> None:
    connection = HTTPConnection(
        {"type": "http", "method": "POST", "headers": [], "client": ("192.0.2.7", 5000)},
    )
    event = await audit.log(
        "user.login",
        outcome="failure",
        actor="a5f1c2",
        reason="wrong password",
        details={"attempts": 3},
        request=connection,
    )

    assert only(trail) is event
    assert (event.outcome, event.reason, event.details) == (
        "failure",
        "wrong password",
        {"attempts": 3},
    )
    assert (event.method, event.route, event.client) == ("POST", None, "192.0.2.7")


async def test_the_trace_it_belongs_to(audit: FastAPIAudit) -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("request") as span:
        event = await audit.log("user.update")

    assert event.trace_id == format(span.get_span_context().trace_id, "032x")
    assert (await audit.log("user.update")).trace_id is None


@pytest.mark.parametrize("name", ["", "-delete", "invoice delete", "a" * 101, "factura/borrar"])
async def test_action_names(audit: FastAPIAudit, name: str) -> None:
    with pytest.raises(ValueError, match="invalid action"):
        audit.action(name)
    with pytest.raises(ValueError, match="invalid action"):
        await audit.log(name)
    with pytest.raises(ValueError, match="invalid action"):
        async with audit.record(name):
            pass


def test_what_an_entry_takes() -> None:
    entry = AuditEntry("invoice.delete", actor=42, resource=uuid.UUID(int=1))
    assert (entry.actor, entry.resource) == ("42", "00000000-0000-0000-0000-000000000001")
    entry.actor = "x" * (MAX_STRING + 1)
    assert entry.actor == "x" * MAX_STRING + "…[1 more]"
    entry.reason = "y" * (MAX_STRING + 2)
    assert entry.reason == "y" * MAX_STRING + "…[2 more]"
    for bad in (True, 1.5, object()):
        with pytest.raises(TypeError, match="the actor is a string"):
            entry.actor = bad  # type: ignore[assignment]
        with pytest.raises(TypeError, match="the resource is a string"):
            entry.resource = bad  # type: ignore[assignment]
    with pytest.raises(ValueError, match="the outcome is one of"):
        entry.outcome = "ok"  # type: ignore[assignment]
    with pytest.raises(TypeError, match="the reason is a string"):
        entry.reason = 404  # type: ignore[assignment]


def test_settings() -> None:
    with pytest.raises(ValueError, match="at least one sink"):
        FastAPIAudit([])
    for timeout in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="timeout"):
            FastAPIAudit(timeout=timeout)
    with pytest.raises(TypeError, match="list of words"):
        FastAPIAudit(redact="iban")
    with pytest.raises(ValueError, match="letters and digits"):
        FastAPIAudit(redact=["account_number"])
    with pytest.raises(ValueError, match="at least 32 bytes"):
        FastAPIAudit(signing_key="short")
    assert isinstance(FastAPIAudit().sinks[0], LoggingAuditSink)


class Colour(enum.Enum):
    RED = "red"


class Size(enum.IntEnum):
    LARGE = 3


async def test_details_keep_no_secrets(audit: FastAPIAudit) -> None:
    secret = {
        "password": "a",
        "newPassword": "b",
        "X-Api-Key": "c",
        "refresh_tokens": ["d"],
        "session_id": "e",
        "privateKey": "f",
        "card_number": "4111111111111111",
        "Authorization": "g",
        "client_secret": "h",
        "set-cookie": "i",
        "otp": "j",
    }
    harmless = {"author": "Ana", "shipping": "express", "footprint": 3, "keys": 2, "pinned": True}
    event = await audit.log("user.update", details={**secret, **harmless})

    assert event.details == {**dict.fromkeys(secret, REDACTED), **harmless}


async def test_values_that_look_like_credentials(audit: FastAPIAudit) -> None:
    details = {
        "header": "Bearer abc123.def",
        "basic": "basic dXNlcjpwYXNz",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
        # Built, not written whole: the private-key hook of pre-commit would take it for one.
        "pem": "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE...",
        "sentence": "The bearer of bad news",
    }
    event = await audit.log("note.add", details=details)

    assert event.details == {
        **dict.fromkeys(list(details)[:4], REDACTED),
        "sentence": details["sentence"],
    }


async def test_words_of_your_own_to_redact(trail: MemoryAuditSink) -> None:
    audit = FastAPIAudit([trail], redact=["IBAN"])
    event = await audit.log(
        "payout.update", details={"iban": "ES91", "customerIban": "ES92", "bank": "B"}
    )

    assert event.details == {"iban": REDACTED, "customerIban": REDACTED, "bank": "B"}


async def test_details_are_json_and_bounded(audit: FastAPIAudit) -> None:
    when = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    event = await audit.log(
        "data.export",
        details={
            "user": object(),
            "blob": b"\x00\x01\x02",
            "when": when,
            "day": when.date(),
            "at": when.time(),
            "id": uuid.UUID(int=2),
            "price": Decimal("1.50"),
            "ratio": float("nan"),
            "share": 0.25,
            "colour": Colour.RED,
            "size": Size.LARGE,
            "tags": {"a"},
            "pair": (1, 2),
            "nested": {"a": {"b": {"c": {"d": 1}}}},
            "many": list(range(MAX_ITEMS + 10)),
            "long": "x" * (MAX_STRING + 100),
            "keys": {1: "int", (1, 2): "tuple", 2.5: "float"},
            "wide": {f"k{index}": index for index in range(MAX_ITEMS + 1)},
        },
    )
    details = event.details

    assert details["user"] == "<object>"
    assert details["blob"] == "<3 bytes>"
    assert (details["when"], details["day"], details["at"]) == (
        when.isoformat(),
        "2026-10-26",
        "12:00:00",
    )
    assert (details["id"], details["price"], details["ratio"]) == (
        str(uuid.UUID(int=2)),
        "1.50",
        "nan",
    )
    assert (details["share"], details["colour"], details["size"]) == (0.25, "red", 3)
    assert type(details["size"]) is int
    assert (details["tags"], details["pair"]) == (["a"], [1, 2])
    assert details["nested"] == {"a": {"b": {"c": "[too deep]"}}}
    assert details["many"] == [*range(MAX_ITEMS), "[10 more]"]
    assert details["long"] == "x" * MAX_STRING + "…[100 more]"
    assert details["keys"] == {"1": "int", "<tuple>": "tuple", "<float>": "float"}
    assert len(details["wide"]) == MAX_ITEMS + 1
    assert details["wide"]["…"] == "[1 more]"
    json.dumps(event.to_dict())  # JSON all the way down


async def test_the_logging_sink(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="fastapi_alcazaba.audit.events")
    event = await FastAPIAudit().log("user.delete", actor="alice", details={"note": "ñ"})

    (record,) = [
        record for record in caplog.records if record.name == "fastapi_alcazaba.audit.events"
    ]
    assert record.levelno == logging.INFO
    assert json.loads(record.getMessage()) == event.to_dict()
    assert record.getMessage().isascii()  # nothing in it splits a line
    assert record.audit == event.to_dict()  # type: ignore[attr-defined]


async def test_the_logging_sink_says_when_events_are_lost(caplog: pytest.LogCaptureFixture) -> None:
    nowhere = logging.getLogger("tests.audit.nowhere")
    nowhere.propagate = False
    nowhere.setLevel(logging.INFO)
    # tests.audit.quiet is under the root logger, whose level lets warnings through only.
    sinks = [LoggingAuditSink(nowhere), LoggingAuditSink("tests.audit.quiet")]
    caplog.set_level(logging.WARNING)  # the root logger's level too
    try:
        audit = FastAPIAudit(sinks)
        await audit.log("user.delete")
        await audit.log("user.delete")
    finally:
        nowhere.propagate = True
        nowhere.setLevel(logging.NOTSET)

    warnings = [
        record.getMessage() for record in caplog.records if record.name == "fastapi_alcazaba.audit"
    ]
    assert len(warnings) == 2  # once per sink
    assert "'tests.audit.nowhere' outputs nothing at level INFO" in warnings[0]
    assert "'tests.audit.quiet'" in warnings[1]


def test_where_log_records_end_up() -> None:
    log = logging.getLogger("tests.audit.where")
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.ERROR)
    log.addHandler(handler)
    log.propagate = False
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        assert not audit_module._outputs(log, logging.INFO)  # the handler takes errors only
        assert audit_module._outputs(log, logging.ERROR)
        log.removeHandler(handler)
        log.propagate = True
        root.handlers.clear()  # no handler at all: logging's last resort, from WARNING up
        assert audit_module._outputs(log, logging.WARNING)
        assert not audit_module._outputs(log, logging.INFO)
    finally:
        root.handlers[:] = saved
        log.removeHandler(handler)
        log.propagate = True
        log.setLevel(logging.NOTSET)


async def test_a_failing_sink_doesn_t_keep_the_event_from_the_others(
    trail: MemoryAuditSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        async def write(self, _event: AuditEvent) -> None:
            raise ConnectionError("database down")

    class Stuck:
        async def write(self, _event: AuditEvent) -> None:
            await asyncio.sleep(10)

    reader = InMemoryMetricReader()
    metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
    audit = FastAPIAudit([Broken(), Stuck(), trail], timeout=0.05, metrics=metrics)
    event = await audit.log("user.delete", actor="alice")

    assert only(trail) is event
    broken, stuck = [record for record in caplog.records if record.name == "fastapi_alcazaba.audit"]
    assert broken.levelno == stuck.levelno == logging.ERROR
    assert "Broken (ConnectionError)" in broken.getMessage()
    assert event.to_json() in broken.getMessage()  # logged, the event isn't lost
    assert broken.exc_info is not None
    assert "Stuck (took longer than 0.05 s)" in stuck.getMessage()
    assert not stuck.exc_info  # a timeout has no traceback worth showing
    counted = metric_points(reader, "fastapi_alcazaba.audit.undelivered")
    assert counted == {
        (("fastapi_alcazaba.reason", "error"), ("fastapi_alcazaba.sink", "Broken")): 1,
        (("fastapi_alcazaba.reason", "timeout"), ("fastapi_alcazaba.sink", "Stuck")): 1,
    }


def metric_points(
    reader: InMemoryMetricReader,
    name: str,
) -> dict[tuple[tuple[str, Any], ...], Any]:
    data = reader.get_metrics_data()
    found = {}
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        found[tuple(sorted((point.attributes or {}).items()))] = getattr(
                            point, "value", None
                        )
    return found


async def test_events_are_counted(trail: MemoryAuditSink) -> None:
    reader = InMemoryMetricReader()
    audit = FastAPIAudit(
        [trail], metrics=Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
    )
    await audit.log("user.delete")
    await audit.log("user.delete", outcome="denied")
    await audit.log("user.delete", outcome="denied")

    assert metric_points(reader, "fastapi_alcazaba.audit") == {
        (("fastapi_alcazaba.action", "user.delete"), ("fastapi_alcazaba.outcome", "success")): 1,
        (("fastapi_alcazaba.action", "user.delete"), ("fastapi_alcazaba.outcome", "denied")): 2,
    }


async def write(audit: FastAPIAudit, count: int) -> list[AuditEvent]:
    return [await audit.log("user.update", resource=f"user:{index}") for index in range(count)]


async def test_signed_events_form_a_chain(audit: FastAPIAudit) -> None:
    first, second, third = await write(audit, 3)

    assert [event.sequence for event in (first, second, third)] == [1, 2, 3]
    assert first.stream == second.stream == third.stream
    assert first.previous is None
    assert (second.previous, third.previous) == (first.signature, second.signature)
    assert verify_audit_trail([third, first, second], KEY) == []
    lines = [json.loads(event.to_json()) for event in (first, second, third)]  # read back from logs
    assert verify_audit_trail(lines, KEY) == []


async def test_without_a_key_events_are_numbered_but_not_signed(trail: MemoryAuditSink) -> None:
    first, second = await write(FastAPIAudit([trail]), 2)

    assert (first.sequence, second.sequence) == (1, 2)
    assert (second.previous, second.signature) == (None, None)
    assert verify_audit_trail([first], KEY) == [f"event {first.id!r} isn't signed"]


async def test_altered_and_forged_events_are_found(audit: FastAPIAudit) -> None:
    first, second = await write(audit, 2)
    altered = {**second.to_dict(), "actor": "mallory"}
    forged = (await write(FastAPIAudit([MemoryAuditSink()], signing_key="m" * 32), 1))[0]

    problems = verify_audit_trail([first, altered, forged], KEY)

    assert problems == [
        f"event {second.id!r} doesn't match its signature: altered, or not written with this key",
        f"event {forged.id!r} doesn't match its signature: altered, or not written with this key",
    ]


async def test_removed_events_are_found(audit: FastAPIAudit) -> None:
    events = await write(audit, 5)

    problems = verify_audit_trail([events[0], events[3], events[4]], KEY)

    assert problems == [f"stream {events[0].stream}: events 2 to 3 are missing"]
    assert verify_audit_trail(events[2:], KEY) == []  # older ones rotated out of the logs


async def test_events_rewritten_with_the_key_break_the_chain(audit: FastAPIAudit) -> None:
    first, second, third = await write(audit, 3)

    def resigned(event: AuditEvent, **changes: Any) -> AuditEvent:
        changed = dataclasses.replace(event, **changes, signature=None)
        data = changed.to_dict()
        del data["signature"]
        return dataclasses.replace(changed, signature=audit_module._mac(KEY.encode(), data))

    swapped = resigned(third, sequence=2)  # the second removed, the third renumbered
    twin = resigned(second, resource="user:99")

    assert verify_audit_trail([first, swapped], KEY) == [
        f"stream {first.stream}: event 2 doesn't follow event 1"
    ]
    assert f"stream {first.stream}: two different events numbered 2" in verify_audit_trail(
        [first, second, twin], KEY
    )


async def test_the_same_event_twice_counts_once(audit: FastAPIAudit) -> None:
    first, second = await write(audit, 2)

    assert verify_audit_trail([first, second, second], KEY) == []


async def test_several_streams(trail: MemoryAuditSink) -> None:
    one = FastAPIAudit([trail], signing_key=KEY)
    other = FastAPIAudit([trail], signing_key=KEY)
    for _ in range(3):
        await one.log("user.update")
        await other.log("user.update")

    assert len({event.stream for event in trail.events}) == 2
    assert verify_audit_trail(trail.events, KEY) == []


async def test_a_rotated_key(trail: MemoryAuditSink) -> None:
    before = await write(FastAPIAudit([trail], signing_key=OLD_KEY), 1)
    after = await write(FastAPIAudit([trail], signing_key=[KEY, OLD_KEY]), 1)

    assert verify_audit_trail([*before, *after], [KEY, OLD_KEY]) == []
    assert len(verify_audit_trail(after, OLD_KEY)) == 1  # the new key signs


async def test_a_new_process_starts_a_new_stream(
    audit: FastAPIAudit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (parent,) = await write(audit, 1)
    monkeypatch.setattr(audit_module, "os", SimpleNamespace(getpid=lambda: -1))  # a fork
    (child,) = await write(audit, 1)

    assert child.stream != parent.stream
    assert (child.sequence, child.previous) == (1, None)
    assert verify_audit_trail([parent, child], KEY) == []


async def test_the_json_form(audit: FastAPIAudit) -> None:
    event = await audit.log("user.update", actor="alice")
    data = json.loads(event.to_json())

    assert data == event.to_dict()
    assert list(data) == [field.name for field in dataclasses.fields(AuditEvent)]
    assert datetime.fromisoformat(data["time"]) == event.time


def test_a_resource_template_with_text_after_its_fields(audit: FastAPIAudit) -> None:
    assert audit.action("invoice.read", resource="invoice:{invoice_id}/pdf") is not None
