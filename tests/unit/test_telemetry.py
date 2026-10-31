"""What the tools record in OpenTelemetry: spans of scheduled jobs, events and attributes on the
request's span, and the counters and histograms of `Metrics.count` and `Metrics.record`."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import fakeredis
import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import (
    BackendUnavailable,
    BreakerOpen,
    Cache,
    FastAPIBreaker,
    FastAPIRateLimiter,
    FastAPIScheduler,
)
from fastapi_alcazaba.auth import APIKeyRecord, FastAPIAPIKeys, OneTimeCodes, TokenBlocklist
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.background import FastAPILeader, FastAPITaskQueue
from fastapi_alcazaba.background.tasks import READ_SCRIPT
from fastapi_alcazaba.idempotency import IdempotencyMiddleware
from fastapi_alcazaba.observability import (
    FastAPIProblemDetails,
    Problem,
    RequestIDMiddleware,
    RequestTimeoutMiddleware,
    health_router,
)
from fastapi_alcazaba.operations import (
    FastAPIFeatureFlags,
    FastAPIMaintenance,
    MaintenanceMiddleware,
)
from fastapi_alcazaba.outbound import (
    BlockedDestinationError,
    CircuitOpenError,
    ResilientTransport,
    SSRFGuardTransport,
)
from fastapi_alcazaba.rate_limit import ConcurrencyLimitExceeded, FastAPIConcurrencyLimiter
from fastapi_alcazaba.response_cache.caches import metrics_of
from fastapi_alcazaba.security import (
    CSRFMiddleware,
    ServerSessionMiddleware,
    clear_session,
    rotate_session,
)
from fastapi_alcazaba.telemetry import Metrics
from fastapi_alcazaba.telemetry.tracing import add_event, set_attributes
from fastapi_alcazaba.uploads import IMAGES, FastAPIUploads
from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookSigner
from tests.unit.test_metrics import points
from tests.unit.test_uploads import png, upload

TICK = 0.1


@pytest.fixture(name="exporter")
def fixture_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture(name="provider")
def fixture_provider(exporter: InMemorySpanExporter) -> TracerProvider:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracer_provider


@pytest.fixture(name="reader")
def fixture_reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture(name="meters")
def fixture_meters(reader: InMemoryMetricReader) -> MeterProvider:
    return MeterProvider([reader])


@pytest.fixture(name="cache")
def fixture_cache(provider: TracerProvider, meters: MeterProvider) -> Cache:
    return Cache(InMemoryBackend(), tracer_provider=provider, meter_provider=meters)


def named(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.name == name]


def counts(reader: InMemoryMetricReader, name: str, *keys: str) -> dict[tuple[Any, ...], Any]:
    return {
        tuple(attributes[f"fastapi_alcazaba.{key}"] for key in keys): value
        for attributes, value in points(reader, f"fastapi_alcazaba.{name}")
    }


class TestScheduledJobs:
    async def test_each_run_is_the_root_of_a_trace(
        self,
        cache: Cache,
        provider: TracerProvider,
        exporter: InMemorySpanExporter,
    ) -> None:
        scheduler = FastAPIScheduler(cache)
        scheduler.every(seconds=TICK, name="report")(noop)
        with provider.get_tracer("app").start_as_current_span("startup"):
            await scheduler.start()  # its tasks would otherwise inherit this span
        await asyncio.sleep(1.5 * TICK)
        await scheduler.stop()
        (run, *_) = named(exporter, "fastapi_alcazaba.scheduler report")
        assert run.parent is None
        assert run.attributes is not None
        assert run.attributes["fastapi_alcazaba.job"] == "report"
        assert "fastapi_alcazaba.tick" in run.attributes

    async def test_failures_are_recorded(
        self,
        cache: Cache,
        exporter: InMemorySpanExporter,
    ) -> None:
        scheduler = FastAPIScheduler(cache)
        scheduler.every(seconds=TICK, name="boom")(boom)
        await scheduler.start()
        await asyncio.sleep(1.5 * TICK)
        await scheduler.stop()
        (run, *_) = named(exporter, "fastapi_alcazaba.scheduler boom")
        assert run.status.status_code == StatusCode.ERROR
        assert [event.name for event in run.events] == ["exception"]

    async def test_skipped_ticks_make_no_spans(
        self,
        provider: TracerProvider,
        exporter: InMemorySpanExporter,
    ) -> None:
        backend = InMemoryBackend()
        schedulers = [FastAPIScheduler(Cache(backend, tracer_provider=provider)) for _ in range(2)]
        for scheduler in schedulers:
            scheduler.every(seconds=TICK, name="once")(noop)
            await scheduler.start()
        await asyncio.sleep(2.5 * TICK)
        for scheduler in schedulers:
            await scheduler.stop()
        runs = named(exporter, "fastapi_alcazaba.scheduler once")
        ticks = [run.attributes["fastapi_alcazaba.tick"] for run in runs if run.attributes]
        assert len(ticks) == len(set(ticks)) >= 2

    async def test_with_tracing_off(
        self,
        provider: TracerProvider,
        exporter: InMemorySpanExporter,
    ) -> None:
        calls: list[int] = []
        quiet = Cache(InMemoryBackend(), tracing=False, tracer_provider=provider)
        scheduler = FastAPIScheduler(quiet)
        scheduler.every(seconds=TICK, name="quiet")(lambda: calls.append(1))
        scheduler.every(seconds=TICK, name="failing")(boom)  # nothing to record it on
        await scheduler.start()
        await asyncio.sleep(1.5 * TICK)
        await scheduler.stop()
        assert calls
        assert not exporter.get_finished_spans()

    async def test_without_any_cache(self, exporter: InMemorySpanExporter) -> None:
        scheduler = FastAPIScheduler()
        scheduler.every(seconds=TICK, name="local", exclusive=False)(noop)
        await scheduler.start()
        await asyncio.sleep(1.5 * TICK)
        await scheduler.stop()  # the global provider's spans: none recorded here
        assert not named(exporter, "fastapi_alcazaba.scheduler local")


class TestEventsOnTheRequestSpan:
    async def test_breaker_refusals_and_transitions(
        self,
        provider: TracerProvider,
        exporter: InMemorySpanExporter,
    ) -> None:
        breaker = FastAPIBreaker("db", failure_threshold=1, reset_timeout=0.05)

        @breaker
        async def query(fail: bool) -> None:
            if fail:
                raise ConnectionError

        with provider.get_tracer("app").start_as_current_span("request"):
            with pytest.raises(ConnectionError):
                await query(fail=True)
            with pytest.raises(BreakerOpen):
                await query(fail=False)
            await asyncio.sleep(0.06)
            await query(fail=False)
        (request,) = named(exporter, "request")
        assert [event.name for event in request.events] == [
            "fastapi_alcazaba.breaker.opened",
            "fastapi_alcazaba.breaker.refused",
            "fastapi_alcazaba.breaker.closed",
        ]
        assert all(event.attributes == {"breaker": "db"} for event in request.events)

    async def test_rate_limits(
        self,
        cache: Cache,
        provider: TracerProvider,
        exporter: InMemorySpanExporter,
    ) -> None:
        limiter = FastAPIRateLimiter(times=1, minutes=1, cache=cache, name="search")
        app = FastAPI()
        tracer = provider.get_tracer("app")

        @app.get("/", dependencies=[Depends(limiter)])
        async def index() -> None:
            pass

        @app.middleware("http")
        async def span(request: Any, call_next: Any) -> Any:  # FastAPI's instrumentation
            with tracer.start_as_current_span("request"):
                return await call_next(request)

        client = TestClient(app)
        assert [client.get("/").status_code for _ in range(2)] == [200, 429]
        allowed, limited = named(exporter, "request")
        assert not allowed.events
        assert [(e.name, e.attributes) for e in limited.events] == [
            ("fastapi_alcazaba.rate_limit", {"outcome": "limited", "limiter": "search"})
        ]

    def test_idempotency(
        self,
        cache: Cache,
        provider: TracerProvider,
        exporter: InMemorySpanExporter,
    ) -> None:
        tracer, app = provider.get_tracer("app"), FastAPI()
        cache.install(app)

        @app.post("/orders", status_code=201)
        async def order() -> dict[str, int]:
            return {"id": 1}

        app.add_middleware(IdempotencyMiddleware)

        class Span:  # FastAPI's instrumentation: the outermost middleware
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                with tracer.start_as_current_span("request"):
                    await self.inner(scope, receive, send)

        client = TestClient(Span(app))
        for _ in range(2):
            client.post("/orders", headers={"Idempotency-Key": "k"})
        outcomes = [
            (s.attributes or {})["fastapi_alcazaba.idempotency"] for s in named(exporter, "request")
        ]
        assert outcomes == ["stored", "replayed"]


class TestIdempotencyMetrics:
    @pytest.fixture
    def client(self, cache: Cache) -> TestClient:
        app = FastAPI()
        cache.install(app)

        @app.post("/orders")
        async def order(fail: bool = False) -> dict[str, int]:
            if fail:
                raise RuntimeError("boom")
            return {"id": 1}

        app.add_middleware(IdempotencyMiddleware, max_body_size=10)
        return TestClient(app, raise_server_exceptions=False)

    def test_outcomes(self, client: TestClient, reader: InMemoryMetricReader) -> None:
        client.post("/orders", headers={"Idempotency-Key": "a"})
        client.post("/orders", headers={"Idempotency-Key": "a"})  # replayed
        client.post("/orders", headers={"Idempotency-Key": "a"}, content=b"other")  # mismatch
        client.post("/orders", headers={"Idempotency-Key": "\x01"})  # invalid
        client.post("/orders", headers={"Idempotency-Key": "b"}, content=b"x" * 11)  # too large
        client.post("/orders?fail=true", headers={"Idempotency-Key": "c"})  # not kept
        assert counts(reader, "idempotency", "outcome") == {
            ("stored",): 1,
            ("replayed",): 1,
            ("mismatch",): 1,
            ("invalid",): 1,
            ("too_large",): 1,
            ("not_kept",): 1,
        }

    def test_backend_failures(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        class Failing(InMemoryBackend):
            async def get(self, key: str) -> bytes | None:
                raise ConnectionError("down")

        app = FastAPI()
        down = Cache(Failing(), meter_provider=meters)
        app.add_middleware(IdempotencyMiddleware, cache=down)

        @app.post("/orders")
        async def order() -> None:
            pass

        TestClient(app).post("/orders", headers={"Idempotency-Key": "a"})
        assert counts(reader, "idempotency", "outcome") == {("unchecked",): 1}

    async def test_conflict(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        sent: list[Any] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("not run")

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Any) -> None:
            sent.append(message)

        middleware = IdempotencyMiddleware(app, cache=cache)
        lock = cache.lock("idempotency:" + digest_of("", "k"), ttl=5)
        assert await lock.acquire()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "headers": [(b"idempotency-key", b"k")],
        }
        await middleware(scope, receive, send)
        await lock.release()
        assert sent[0]["status"] == 409
        assert counts(reader, "idempotency", "outcome") == {("conflict",): 1}

    def test_without_a_cache_configured(self) -> None:
        app = FastAPI()
        app.add_middleware(IdempotencyMiddleware)
        refused = TestClient(app).post("/", headers={"Idempotency-Key": "\x01"})
        assert refused.status_code == 400  # nothing to count with


def receiving(webhooks: FastAPIWebhooks) -> FastAPI:
    """An app that receives webhooks at /hooks."""
    app = FastAPI()

    @app.post("/hooks", dependencies=[Depends(webhooks)])
    async def hook() -> None:
        pass

    return app


def digest_of(identity: str, key: str) -> str:
    return hashlib.sha256(json.dumps([identity, key]).encode()).hexdigest()[:32]


class TestLockMetrics:
    async def test_waits_and_lost_locks(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        held = cache.lock("job", ttl=0.1)
        assert await held.acquire()
        assert not await cache.lock("job", wait=0.05).acquire()
        waits = counts(reader, "lock.wait", "acquired")
        assert set(waits) == {("true",), ("false",)}
        cache.backend._locks.clear()  # type: ignore[attr-defined]  # it expired meanwhile
        await asyncio.sleep(0.1)
        assert not held.held
        assert points(reader, "fastapi_alcazaba.lock.lost") == [({}, 1)]
        await held.release()


class TestTokenMetrics:
    async def test_revocation_checks(
        self,
        cache: Cache,
        meters: MeterProvider,
        reader: InMemoryMetricReader,
    ) -> None:
        blocklist = TokenBlocklist(cache)
        await blocklist.revoke("old", ttl=60)
        assert await blocklist.is_revoked("old")
        assert not await blocklist.is_revoked("new")
        down = TokenBlocklist(Cache(Down(), meter_provider=meters))
        assert await down.is_revoked("any")
        assert counts(reader, "tokens.checks", "outcome") == {
            ("revoked",): 1,
            ("valid",): 1,
            ("unchecked",): 1,
        }

    async def test_one_time_codes(
        self,
        cache: Cache,
        meters: MeterProvider,
        reader: InMemoryMetricReader,
    ) -> None:
        codes = OneTimeCodes("s" * 32, cache, max_attempts=3)
        assert not await codes.verify("alice", "123456")  # missing
        code = await codes.issue("alice")
        assert not await codes.verify("alice", "12")  # malformed
        assert not await codes.verify("alice", other(code))  # wrong
        assert await codes.verify("alice", code)  # success
        code = await codes.issue("bob")
        for _ in range(3):
            await codes.verify("bob", other(code))
        assert not await codes.verify("bob", code)  # exhausted
        down = OneTimeCodes("s" * 32, Cache(Down(), meter_provider=meters))
        assert not await down.verify("alice", code)  # unchecked
        assert counts(reader, "otp.verifications", "outcome") == {
            ("missing",): 1,
            ("malformed",): 1,
            ("wrong",): 4,
            ("success",): 1,
            ("exhausted",): 1,
            ("unchecked",): 1,
        }


def other(code: str) -> str:
    return f"{(int(code) + 1) % 10 ** len(code):0{len(code)}d}"


class Down(InMemoryBackend):
    async def get(self, key: str) -> bytes | None:
        raise ConnectionError("down")

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        raise ConnectionError("down")


class TestHealthMetrics:
    def test_each_check(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        app = FastAPI()

        async def broken() -> None:
            raise ConnectionError

        app.include_router(health_router({"db": noop, "search": broken}, cache=cache))
        assert TestClient(app).get("/health/ready").status_code == 503
        assert counts(reader, "health.checks", "check", "status") == {
            ("db", "ok"): 1,
            ("search", "fail"): 1,
            ("cache", "ok"): 1,
        }
        durations = counts(reader, "health.check.duration", "check")
        assert set(durations) == {("db",), ("search",), ("cache",)}

    def test_without_a_cache(self) -> None:
        app = FastAPI()
        app.include_router(health_router({"db": noop}, include_cache=False))
        assert TestClient(app).get("/health/ready").status_code == 200

    def test_with_the_apps_cache(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        app = FastAPI()
        cache.install(app)
        app.include_router(health_router({"db": noop}, include_cache=False))
        TestClient(app).get("/health/ready")
        assert counts(reader, "health.checks", "check", "status") == {("db", "ok"): 1}


class TestMetricsAPI:
    def test_unknown_instruments(self) -> None:
        metrics = Metrics(enabled=False)
        with pytest.raises(ValueError, match="unknown counter"):
            metrics.count("made-up")
        with pytest.raises(ValueError, match="unknown histogram"):
            metrics.record("made-up", 1.0)
        metrics.count("lock.lost")  # disabled: nothing happens
        metrics.record("lock.wait", 1.0)

    def test_dotted_attributes_keep_their_names(
        self,
        meters: MeterProvider,
        reader: InMemoryMetricReader,
    ) -> None:
        metrics = Metrics(meter_provider=meters)
        metrics.count("outbound.breaker", event="opened", **{"server.address": "api.example.com"})
        metrics.count("outbound.breaker", event="opened", **{"server.address": "api.example.com"})
        assert points(reader, "fastapi_alcazaba.outbound.breaker") == [
            ({"fastapi_alcazaba.event": "opened", "server.address": "api.example.com"}, 2)
        ]

    def test_span_helpers_without_a_recording_span(self) -> None:
        add_event("nothing")  # no current span: no-ops
        set_attributes(outcome="none")


async def noop() -> None:
    pass


async def boom() -> None:
    raise RuntimeError("boom")


class TestHTTPPlumbingMetrics:
    def test_problems(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        app = FastAPI()
        cache.install(app)
        FastAPIProblemDetails().install(app)

        @app.get("/items/{item_id}")
        async def item(item_id: int) -> None:
            if item_id == 1:
                raise Problem(409, "Out of stock")
            raise RuntimeError("boom")

        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/missing").status_code == 404
        assert client.get("/items/1").status_code == 409
        assert client.get("/items/x").status_code == 422
        assert client.get("/items/2").status_code == 500
        assert counts(reader, "problems", "status", "kind") == {
            ("404", "http_exception"): 1,
            ("409", "problem"): 1,
            ("422", "validation"): 1,
            ("500", "unhandled"): 1,
        }

    def test_request_ids(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        app = FastAPI()
        cache.install(app)
        app.add_middleware(RequestIDMiddleware)

        @app.get("/")
        async def index() -> None:
            pass

        client = TestClient(app)
        client.get("/")
        client.get("/", headers={"X-Request-ID": "abc-123"})
        client.get("/", headers={"X-Request-ID": "bad id"})
        assert counts(reader, "request_ids", "source") == {
            ("generated",): 1,
            ("incoming",): 1,
            ("rejected",): 1,
        }

    def test_request_timeouts(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        app = FastAPI()
        cache.install(app)
        app.add_middleware(RequestTimeoutMiddleware, timeout=0.05)

        @app.get("/slow")
        async def slow() -> None:
            await asyncio.sleep(1)

        @app.get("/stream")
        async def stream() -> StreamingResponse:
            async def chunks() -> AsyncIterator[bytes]:
                yield b"first"
                await asyncio.sleep(1)
                yield b"never"

            return StreamingResponse(chunks())

        client = TestClient(app)
        assert client.get("/slow").status_code == 504
        with pytest.raises(TimeoutError):
            client.get("/stream")
        assert counts(reader, "request_timeouts", "outcome", "route") == {
            ("504", "/slow"): 1,
            ("cut", "/stream"): 1,
        }

    def test_without_an_app_or_a_cache(self) -> None:
        assert not metrics_of(HTTPConnection({"type": "http", "headers": []})).enabled
        app = FastAPI()
        assert not metrics_of(HTTPConnection({"type": "http", "headers": [], "app": app})).enabled


class FlakyLocks(InMemoryBackend):
    failing = False

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        if self.failing:
            raise ConnectionError("down")
        return await super().acquire_lock(key, token, ttl)


class TestCoordinationMetrics:
    async def test_concurrency(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        backend = FlakyLocks()
        cache = Cache(backend, meter_provider=meters)
        limiter = FastAPIConcurrencyLimiter(1, name="ocr", cache=cache)
        strict = FastAPIConcurrencyLimiter(1, name="pdf", cache=cache, fail_open=False)
        async with limiter.slot():
            with pytest.raises(ConcurrencyLimitExceeded):
                async with limiter.slot():
                    pass
        backend.failing = True
        async with limiter.slot():  # counted in this process
            pass
        with pytest.raises(BackendUnavailable):
            async with strict.slot():
                pass
        assert counts(reader, "concurrency", "outcome", "limiter") == {
            ("acquired", "ocr"): 1,
            ("refused", "ocr"): 1,
            ("local", "ocr"): 1,
            ("unavailable", "pdf"): 1,
        }
        assert not points(reader, "fastapi_alcazaba.lock.wait")  # slots aren't measured

    async def test_leader(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        cache = Cache(InMemoryBackend(), meter_provider=meters)
        leader = FastAPILeader("relay", ttl=0.3, cache=cache)
        await leader.start()
        await until(lambda: leader.is_leader)
        cache.backend._locks.clear()  # type: ignore[attr-defined]  # it expired meanwhile
        await until(lambda: not leader.is_leader)
        await until(lambda: leader.is_leader)  # elected again
        await leader.stop()
        assert counts(reader, "leader.transitions", "leader", "event") == {
            ("relay", "elected"): 2,
            ("relay", "lost"): 1,
            ("relay", "resigned"): 1,
        }
        assert not points(reader, "fastapi_alcazaba.lock.wait")

    async def test_maintenance(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        maintenance = FastAPIMaintenance(cache)
        app = FastAPI()
        app.add_middleware(MaintenanceMiddleware, maintenance=maintenance)

        @app.get("/")
        async def index() -> None:
            pass

        await maintenance.enable()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/")).status_code == 503
        assert points(reader, "fastapi_alcazaba.maintenance.refused") == [({}, 1)]

    async def test_flags(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        flags = FastAPIFeatureFlags(cache)
        await flags.set("beta", enabled=True)
        await flags.set("legacy", enabled=False)
        assert await flags.enabled("beta")
        assert not await flags.enabled("legacy")
        assert not await flags.enabled("unknown")
        assert counts(reader, "flags.evaluations", "flag", "result") == {
            ("beta", "on"): 1,
            ("legacy", "off"): 1,
            ("unknown", "default"): 1,
        }


async def until(condition: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class TestIntegrationMetrics:
    def test_webhooks(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        secret = WebhookSigner.generate_secret()
        signer = WebhookSigner(secret)
        app = FastAPI()
        Cache(InMemoryBackend(), meter_provider=meters).install(app)
        webhooks = FastAPIWebhooks(secret, max_body_size=100)

        @app.post("/hooks", dependencies=[Depends(webhooks)])
        async def hook() -> None:
            pass

        client = TestClient(app)
        body = b'{"event": "paid"}'
        headers = signer.sign(body)
        assert client.post("/hooks", content=body, headers=headers).status_code == 200
        assert client.post("/hooks", content=body, headers=headers).status_code == 401  # replay
        forged = {**signer.sign(body), "webhook-signature": "v1,AAAA"}
        assert client.post("/hooks", content=body, headers=forged).status_code == 401
        assert client.post("/hooks", content=body).status_code == 400
        big = b"x" * 101
        assert client.post("/hooks", content=big, headers=signer.sign(big)).status_code == 413
        assert counts(reader, "webhooks", "outcome") == {
            ("accepted",): 1,
            ("replayed",): 1,
            ("rejected",): 1,
            ("malformed",): 1,
            ("too_large",): 1,
        }

    def test_webhooks_with_the_backend_down(
        self,
        meters: MeterProvider,
        reader: InMemoryMetricReader,
    ) -> None:
        secret = WebhookSigner.generate_secret()
        signer, body = WebhookSigner(secret), b"{}"
        for fail_open in (False, True):
            down = Cache(Down(), meter_provider=meters)
            app = receiving(FastAPIWebhooks(secret, cache=down, fail_open=fail_open))
            TestClient(app).post("/hooks", content=body, headers=signer.sign(body))
        assert counts(reader, "webhooks", "outcome") == {("unavailable",): 1, ("unchecked",): 1}

    async def test_api_keys(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        records: dict[str, APIKeyRecord] = {}
        keys: FastAPIAPIKeys[APIKeyRecord] = FastAPIAPIKeys(
            records.get, metrics=Metrics(meter_provider=meters)
        )
        good = keys.generate(scopes=["orders:read"])
        old = keys.generate(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        for new in (good, old):
            records[new.record.id] = new.record
        assert await keys.authenticate(good.key)
        for key in ("nonsense", keys.generate().key, good.key[:-1] + "x", old.key):
            with pytest.raises(HTTPException):
                await keys.authenticate(key)
        app = FastAPI()

        @app.get("/orders", dependencies=[Depends(keys.require("orders:write"))])
        async def orders() -> None:
            pass

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            refused = await client.get("/orders", headers={"X-API-Key": good.key})
        assert refused.status_code == 403
        assert counts(reader, "api_keys", "outcome") == {
            ("accepted",): 2,
            ("malformed",): 1,
            ("unknown",): 1,
            ("wrong_secret",): 1,
            ("expired",): 1,
            ("insufficient_scope",): 1,
        }

    async def test_outbound_breakers(
        self,
        meters: MeterProvider,
        reader: InMemoryMetricReader,
        provider: TracerProvider,
    ) -> None:
        async def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        transport = ResilientTransport(
            httpx.MockTransport(broken), failure_threshold=1, metrics=Metrics(meter_provider=meters)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get("https://api.example.com/")).status_code == 503
            with provider.get_tracer("app").start_as_current_span("request"):
                with pytest.raises(CircuitOpenError):
                    await client.get("https://api.example.com/")
        assert counts(reader, "outbound.breaker", "event") == {("opened",): 1, ("refused",): 1}
        assert {a["server.address"] for a, _ in points(reader, OUTBOUND)} == {"api.example.com"}

    async def test_ssrf_blocks(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        async def never(request: httpx.Request) -> httpx.Response:
            raise AssertionError("not sent")

        guard = SSRFGuardTransport(
            httpx.MockTransport(never), ports={443}, metrics=Metrics(meter_provider=meters)
        )
        async with httpx.AsyncClient(transport=guard) as client:
            for url in ("ftp://example.com/", "https://127.0.0.1/", "https://8.8.8.8:8443/"):
                with pytest.raises(BlockedDestinationError):
                    await client.get(url)
        assert counts(reader, "outbound.ssrf.blocked", "reason") == {
            ("scheme",): 1,
            ("address",): 1,
            ("port",): 1,
        }


OUTBOUND = "fastapi_alcazaba.outbound.breaker"


class TestBrowserMetrics:
    def test_csrf_refusals(self, cache: Cache, reader: InMemoryMetricReader) -> None:
        app = FastAPI()
        cache.install(app)
        app.add_middleware(CSRFMiddleware, require_token=True)

        @app.post("/form")
        async def form() -> None:
            pass

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.close()

        cookie = {"cookie": "sid=1"}
        for headers in (
            {"sec-fetch-site": "cross-site"},
            {"origin": "https://evil.example"},
            {},
            {"sec-fetch-site": "same-origin"},  # but without the token
        ):
            client = TestClient(app, base_url="https://testserver")
            assert client.post("/form", headers={**cookie, **headers}).status_code == 403
        with pytest.raises(WebSocketDisconnect):  # noqa: PT012
            with TestClient(app).websocket_connect(
                "/ws", headers={**cookie, "origin": "https://evil.example"}
            ):
                pass
        assert counts(reader, "csrf.refused", "kind", "reason") == {
            ("http", "fetch_site"): 1,
            ("http", "origin"): 1,
            ("http", "no_source"): 1,
            ("http", "token"): 1,
            ("websocket", "origin"): 1,
        }

    def test_sessions(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        backend = InMemoryBackend()
        app = FastAPI()
        Cache(backend, meter_provider=meters).install(app)
        app.add_middleware(ServerSessionMiddleware)

        @app.post("/set")
        async def set_value(request: Request, value: str) -> None:
            request.session["value"] = value

        @app.post("/login")
        async def login(request: Request) -> None:
            rotate_session(request)

        @app.post("/logout")
        async def logout(request: Request) -> None:
            clear_session(request)

        client = TestClient(app, base_url="https://testserver")
        client.post("/set?value=a")
        client.post("/set?value=b")
        client.post("/login")
        client.post("/logout")
        forged = "f" * 43
        cache = Cache(backend)
        key = cache.state_key("session", hashlib.sha256(forged.encode()).hexdigest())
        asyncio.run(backend.set(key, b"not a session", 60))
        client.cookies.set("__Host-session", forged)
        client.post("/set?value=c")
        assert counts(reader, "sessions", "event") == {
            ("created",): 2,
            ("updated",): 1,
            ("rotated",): 1,
            ("deleted",): 1,
            ("rejected",): 1,
        }

    def test_sessions_with_the_backend_down(
        self,
        meters: MeterProvider,
        reader: InMemoryMetricReader,
    ) -> None:
        app = FastAPI()
        Cache(Down(), meter_provider=meters).install(app)
        app.add_middleware(ServerSessionMiddleware)

        @app.post("/set")
        async def set_value(request: Request) -> None:
            request.session["value"] = "a"

        client = TestClient(app, base_url="https://testserver")
        client.cookies.set("__Host-session", "s" * 43)
        assert client.post("/set").status_code == 503
        assert counts(reader, "sessions", "event") == {("unreadable",): 1, ("unavailable",): 1}

    async def test_uploads(self, meters: MeterProvider, reader: InMemoryMetricReader) -> None:
        uploads = FastAPIUploads(
            IMAGES, max_size=1024, max_pixels=10_000, metrics=Metrics(meter_provider=meters)
        )
        await uploads.check(upload(png(10, 10)))
        for data in (b"", b"x" * 2000, b"%PDF-1.7", png(1000, 1000), png(0, 10)):
            with pytest.raises(HTTPException):
                await uploads.check(upload(data))
        assert counts(reader, "uploads", "outcome") == {
            ("accepted",): 1,
            ("empty",): 1,
            ("too_large",): 1,
            ("unsupported",): 1,
            ("too_many_pixels",): 1,
            ("unreadable",): 1,
        }
        sizes = points(reader, "fastapi_alcazaba.uploads.size")
        assert len(sizes) == 1
        attributes, accepted = sizes[0]
        assert attributes == {"fastapi_alcazaba.content_type": "image/png"}
        assert accepted == 1

    async def test_uploads_count_on_fastapicache_by_default(self) -> None:
        await FastAPIUploads(IMAGES).check(upload(png()))  # no cache: disabled metrics


class Flaky(Exception):
    pass


class TestTaskTelemetry:
    @pytest.fixture
    def server(self) -> fakeredis.FakeServer:
        return fakeredis.FakeServer()

    @pytest.fixture
    def queue(
        self,
        server: fakeredis.FakeServer,
        provider: TracerProvider,
        meters: MeterProvider,
    ) -> FastAPITaskQueue:
        cache = Cache(
            RedisBackend(fakeredis.FakeAsyncRedis(server=server)),
            signing_key="s" * 32,
            tracer_provider=provider,
            meter_provider=meters,
        )
        return FastAPITaskQueue(cache, name="mail", poll_interval=0.02, visibility_timeout=0.3)

    async def test_runs_are_traced_and_linked_to_the_enqueuer(
        self,
        queue: FastAPITaskQueue,
        provider: TracerProvider,
        exporter: InMemorySpanExporter,
    ) -> None:
        done = asyncio.Event()

        @queue.task("welcome")
        # pylint: disable-next=unused-argument  # the task's signature
        async def welcome(user_id: int) -> None:
            done.set()

        with provider.get_tracer("app").start_as_current_span("POST /users") as request:
            task_id = await queue.enqueue(welcome, 7)
        async with queue:
            await asyncio.wait_for(done.wait(), 3)
            await until(lambda: not queue._running)
        (run,) = named(exporter, "fastapi_alcazaba.task welcome")
        assert run.parent is None
        assert run.kind == SpanKind.CONSUMER
        assert [link.context.span_id for link in run.links] == [request.get_span_context().span_id]
        assert run.attributes == {
            "fastapi_alcazaba.queue": "mail",
            "fastapi_alcazaba.task": "welcome",
            "fastapi_alcazaba.task_id": task_id,
            "fastapi_alcazaba.attempt": 0,
        }

    async def test_without_an_enqueuing_span(
        self,
        queue: FastAPITaskQueue,
        exporter: InMemorySpanExporter,
    ) -> None:
        done = asyncio.Event()

        @queue.task("report")
        async def report() -> None:
            done.set()

        await queue.enqueue(report)
        async with queue:
            await asyncio.wait_for(done.wait(), 3)
            await until(lambda: not queue._running)
        (run,) = named(exporter, "fastapi_alcazaba.task report")
        assert not run.links

    async def test_with_tracing_off(
        self,
        server: fakeredis.FakeServer,
        exporter: InMemorySpanExporter,
    ) -> None:
        backend = RedisBackend(fakeredis.FakeAsyncRedis(server=server))
        cache = Cache(backend, signing_key="s" * 32, tracing=False)
        queue = FastAPITaskQueue(cache, poll_interval=0.02)
        ran: list[str] = []

        @queue.task("quiet")
        async def quiet() -> None:
            ran.append("quiet")

        @queue.task("failing", retries=0)
        async def failing() -> None:
            raise Flaky  # nothing to record it on

        await queue.enqueue(quiet)
        await queue.enqueue(failing)
        async with queue:
            await until_settled(queue)
        assert ran == ["quiet"]
        assert not exporter.get_finished_spans()

    async def test_a_link_that_isnt_one(self, provider: TracerProvider) -> None:
        tracer = Cache(InMemoryBackend(), tracer_provider=provider).tracer
        with tracer.task("q", "t", "id", 0, "00-garbage") as span:
            assert not span.links

    async def test_outcomes(
        self,
        queue: FastAPITaskQueue,
        server: fakeredis.FakeServer,
        reader: InMemoryMetricReader,
        exporter: InMemorySpanExporter,
    ) -> None:
        @queue.task("ok")
        async def ok() -> None:
            pass

        @queue.task("flaky", retries=1, backoff=0.01)
        async def flaky() -> None:
            raise Flaky

        raw: Any = fakeredis.FakeAsyncRedis(server=server)
        await queue.enqueue(ok)
        await queue.enqueue(flaky)
        queue._ready()
        await raw.xadd(queue._stream, {"p": b"planted"})
        async with queue:
            await until_settled(queue)
        assert counts(reader, "tasks.enqueued", "queue", "task") == {
            ("mail", "ok"): 1,
            ("mail", "flaky"): 1,
        }
        found = {
            (a.get("fastapi_alcazaba.task"), a["fastapi_alcazaba.outcome"]): n
            for a, n in points(reader, "fastapi_alcazaba.tasks")
        }
        assert found == {
            ("ok", "succeeded"): 1,
            ("flaky", "retried"): 1,
            ("flaky", "failed"): 1,
            (None, "tampered"): 1,  # no name: anyone could have written it
        }
        durations = {
            a["fastapi_alcazaba.outcome"]: n
            for a, n in points(reader, "fastapi_alcazaba.tasks.duration")
        }
        assert durations == {"succeeded": 1, "retried": 1, "failed": 1}
        failed = named(exporter, "fastapi_alcazaba.task flaky")
        assert [span.status.status_code for span in failed] == [StatusCode.ERROR] * 2
        attempts = [
            span.attributes["fastapi_alcazaba.attempt"] for span in failed if span.attributes
        ]
        assert sorted(attempts, key=str) == [0, 1]

    async def test_interrupted_and_reclaimed(
        self,
        queue: FastAPITaskQueue,
        server: fakeredis.FakeServer,
        reader: InMemoryMetricReader,
    ) -> None:
        started = asyncio.Event()

        @queue.task("slow")
        async def slow() -> None:
            started.set()
            await asyncio.sleep(10)

        raw: Any = fakeredis.FakeAsyncRedis(server=server)
        await queue.enqueue(slow)
        queue._ready()
        await raw.xgroup_create(queue._stream, "workers", id="0", mkstream=True)
        assert await raw.eval(READ_SCRIPT, 1, queue._stream, "workers", "dead-worker", "10")
        queue.grace = 0.05
        async with queue:  # takes it over from the dead worker, and stops before it ends
            await asyncio.wait_for(started.wait(), 3)
        assert counts(reader, "tasks.reclaimed", "queue") == {("mail",): 1}
        assert counts(reader, "tasks", "task", "outcome") == {("slow", "interrupted"): 1}


async def until_settled(queue: FastAPITaskQueue, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while await queue.pending() or queue._running:
        if loop.time() > deadline:
            raise AssertionError("tasks still pending")
        await asyncio.sleep(0.02)
