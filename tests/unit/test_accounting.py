"""FastAPIAccounting: what each subject uses per period, its quotas, and the usage records."""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketException
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import Cache
from fastapi_alcazaba.accounting import (
    FastAPIAccounting,
    LoggingUsageSink,
    MemoryUsageSink,
    Metering,
    Quota,
    UsageRecord,
    period_of,
)
from fastapi_alcazaba.accounting import meter as meter_module
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.observability import FastAPIProblemDetails
from fastapi_alcazaba.telemetry import Metrics
from tests.unit.test_metrics import points

NOW = datetime(2026, 11, 14, 10, 30, tzinfo=UTC)


def at(moment: datetime) -> type[datetime]:
    """A datetime whose now() is `moment`, for the module's clock."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Frozen":
            return cls.fromtimestamp(moment.timestamp(), tz)

    return Frozen


@pytest.fixture(name="clock", autouse=True)
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    """The clock is at NOW unless a test moves it."""

    def move(moment: datetime) -> None:
        monkeypatch.setattr(meter_module, "datetime", at(moment))

    move(NOW)
    return move


def tenant(x_tenant: Annotated[str | None, Header()] = None) -> str | None:
    """Stands in for the app's authentication: the tenant the request says it is."""
    return x_tenant


def as_tenant(name: str) -> dict[str, str]:
    return {"X-Tenant": name}


class Setup:
    """An accounting, its cache and records, and an app with a route per way of metering."""

    def __init__(self, *quotas: Quota, **options: Any) -> None:
        self.cache = Cache(options.pop("backend", None) or InMemoryBackend())
        self.sink = MemoryUsageSink()
        self.accounting = FastAPIAccounting(
            subject=tenant,
            quotas=quotas,
            cache=self.cache,
            sinks=options.pop("sinks", [self.sink]),
            **options,
        )
        app = FastAPI()
        accounting = self.accounting

        @app.get("/search", dependencies=[Depends(accounting.meter("requests"))])
        async def search() -> list[str]:
            return ["olive oil"]

        @app.get("/export")
        async def export(
            subject: Annotated[str, Depends(accounting.meter("rows", amount=5))],
        ) -> str:
            return subject

        @app.post("/answers")
        async def answer(
            tokens: int,
            usage: Annotated[Metering, Depends(accounting.meter("tokens", amount=None))],
        ) -> dict[str, Any]:
            usage.add(tokens)
            return {"remaining": usage.remaining}

        @app.get("/fails/{status}", dependencies=[Depends(accounting.meter("requests"))])
        async def fails(status: int) -> None:
            if status == 0:
                raise RuntimeError("a bug")
            raise HTTPException(status)

        @app.post("/answers/fail")
        async def answer_then_fail(
            usage: Annotated[Metering, Depends(accounting.meter("tokens", amount=None))],
        ) -> None:
            usage.add(7)
            raise RuntimeError("after using 7 tokens")

        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)

    async def used(self, subject: str, meter: str) -> int:
        return (await self.accounting.usage(subject, meter)).used

    def charged(self) -> list[tuple[str, str, int]]:
        return [(record.subject, record.meter, record.amount) for record in self.sink.records]


class TestPeriods:
    @pytest.mark.parametrize(
        ("period", "label", "start", "end"),
        [
            ("hour", "2026-11-14T10", datetime(2026, 11, 14, 10), datetime(2026, 11, 14, 11)),
            ("day", "2026-11-14", datetime(2026, 11, 14), datetime(2026, 11, 15)),
            ("month", "2026-11", datetime(2026, 11, 1), datetime(2026, 12, 1)),
        ],
    )
    def test_calendar_periods_in_utc(
        self,
        period: Any,
        label: str,
        start: datetime,
        end: datetime,
    ) -> None:
        assert period_of(period, NOW) == (label, start.replace(tzinfo=UTC), end.replace(tzinfo=UTC))

    def test_months_of_every_length(self) -> None:
        assert period_of("month", datetime(2026, 12, 31, 23, tzinfo=UTC))[2] == datetime(
            2027, 1, 1, tzinfo=UTC
        )
        assert period_of("month", datetime(2028, 2, 10, tzinfo=UTC))[2] == datetime(
            2028, 3, 1, tzinfo=UTC
        )

    def test_another_time_zone_is_read_in_utc(self) -> None:
        """Half past midnight of December 1 in Madrid is still November in UTC."""
        madrid = datetime(2026, 12, 1, 0, 30, tzinfo=ZoneInfo("Europe/Madrid"))
        assert period_of("month", madrid)[0] == "2026-11"
        assert period_of("day", madrid)[0] == "2026-11-30"


class TestConfiguration:
    @pytest.mark.parametrize(
        ("options", "error", "message"),
        [
            ({"meter": "Requests"}, ValueError, "a meter is a name"),
            ({"meter": ""}, ValueError, "a meter is a name"),
            ({"limit": -1}, ValueError, "can't be negative"),
            ({"limit": True}, TypeError, "limit is a number"),
            ({"limit": "100"}, TypeError, "limit is a number"),
            ({"period": "week"}, ValueError, "the period is one of hour, day, month"),
            ({"status": 403}, ValueError, "429"),
        ],
    )
    def test_quota(self, options: dict[str, Any], error: type[Exception], message: str) -> None:
        with pytest.raises(error, match=message):
            Quota(**{"meter": "requests", "limit": 10, **options})

    def test_accounting(self) -> None:
        with pytest.raises(ValueError, match="two quotas"):
            FastAPIAccounting(subject=tenant, quotas=[Quota("a", 1), Quota("a", 2)])
        with pytest.raises(ValueError, match="the period is one of"):
            FastAPIAccounting(subject=tenant, period="year")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="timeout"):
            FastAPIAccounting(subject=tenant, timeout=0)
        accounting = FastAPIAccounting(subject=tenant)
        assert isinstance(accounting.sinks[0], LoggingUsageSink)
        with pytest.raises(ValueError, match="a meter is a name"):
            accounting.meter("API calls")
        with pytest.raises(ValueError, match="amount is a number of units"):
            accounting.meter("requests", amount=0)

    def test_a_backend_without_counters(self) -> None:
        class NoCounters(InMemoryBackend):
            supports_counters = False

        client = Setup(backend=NoCounters()).client
        with pytest.raises(NotImplementedError, match="NoCounters can't keep counters"):
            TestClient(client.app).get("/search", headers=as_tenant("acme"))


class TestCharges:
    async def test_each_request_is_counted_for_its_subject(self) -> None:
        setup = Setup()
        for name in ("acme", "acme", "initech"):
            assert setup.client.get("/search", headers=as_tenant(name)).status_code == 200
        assert setup.client.get("/export", headers=as_tenant("acme")).json() == "acme"
        assert await setup.used("acme", "requests") == 2
        assert await setup.used("initech", "requests") == 1
        assert await setup.used("acme", "rows") == 5
        assert setup.charged() == [
            ("acme", "requests", 1),
            ("acme", "requests", 1),
            ("initech", "requests", 1),
            ("acme", "rows", 5),
        ]

    def test_without_a_subject_a_401(self) -> None:
        setup = Setup(challenge="Basic")
        refused = setup.client.get("/search")
        assert (refused.status_code, refused.headers["WWW-Authenticate"]) == (401, "Basic")
        assert setup.charged() == []

    def test_the_record(self) -> None:
        setup = Setup()
        setup.client.get("/search", headers=as_tenant("acme"))
        (record,) = setup.sink.records
        assert (record.method, record.route, record.period) == ("GET", "/search", "2026-11")
        assert record.time == NOW
        assert len(record.id) == 32
        data = json.loads(record.to_json())
        assert data["time"] == "2026-11-14T10:30:00+00:00"
        assert data["subject"] == "acme"

    @pytest.mark.parametrize("status", [400, 404, 409, 422])
    async def test_client_errors_are_charged(self, status: int) -> None:
        setup = Setup()
        assert setup.client.get(f"/fails/{status}", headers=as_tenant("acme")).status_code == status
        assert await setup.used("acme", "requests") == 1
        assert setup.charged() == [("acme", "requests", 1)]

    @pytest.mark.parametrize("status", [0, 500, 503])
    async def test_server_errors_are_refunded(self, status: int) -> None:
        setup = Setup()
        assert setup.client.get(f"/fails/{status}", headers=as_tenant("acme")).status_code >= 500
        assert await setup.used("acme", "requests") == 0
        assert setup.charged() == []

    async def test_a_validation_error_is_charged(self) -> None:
        setup = Setup()
        assert setup.client.get("/fails/x", headers=as_tenant("acme")).status_code == 422
        assert await setup.used("acme", "requests") == 1

    @pytest.mark.parametrize("subject", [42, uuid.UUID(int=7)])
    async def test_ids_that_arent_strings(self, subject: int | uuid.UUID) -> None:
        def fixed() -> int | uuid.UUID:
            return subject

        accounting = FastAPIAccounting(subject=fixed, cache=Cache(InMemoryBackend()), sinks=())
        app = FastAPI()
        app.get("/", dependencies=[Depends(accounting.meter("requests"))])(lambda: None)
        TestClient(app).get("/")
        assert (await accounting.usage(subject, "requests")).used == 1

    @pytest.mark.parametrize("subject", [True, 4.2, {"id": 1}, "", "x" * 201, "a\nb"])
    def test_a_subject_that_isnt_an_id(self, subject: Any) -> None:
        accounting = FastAPIAccounting(
            subject=lambda: subject, cache=Cache(InMemoryBackend()), sinks=()
        )
        app = FastAPI()
        app.get("/", dependencies=[Depends(accounting.meter("requests"))])(lambda: None)
        with pytest.raises((TypeError, ValueError), match="ID"):
            TestClient(app).get("/")


class TestQuotas:
    async def test_over_the_quota_a_429_until_the_period_ends(self) -> None:
        setup = Setup(Quota("requests", limit=2))
        statuses = [setup.client.get("/search", headers=as_tenant("acme")) for _ in range(3)]
        assert [response.status_code for response in statuses] == [200, 200, 429]
        refused = statuses[2]
        assert refused.json() == {"detail": "Quota exceeded"}
        until_december = int((datetime(2026, 12, 1, tzinfo=UTC) - NOW).total_seconds())
        assert refused.headers["Retry-After"] == str(until_december)
        assert refused.headers["RateLimit"] == f'"requests";r=0;t={until_december}'
        assert await setup.used("acme", "requests") == 2  # the refused one isn't counted
        assert setup.client.get("/search", headers=as_tenant("initech")).status_code == 200

    def test_the_headers_say_what_is_left(self) -> None:
        setup = Setup(Quota("requests", limit=10, period="day"))
        response = setup.client.get("/search", headers=as_tenant("acme"))
        assert response.headers["RateLimit"] == '"requests";r=9;t=48600'
        assert response.headers["RateLimit-Policy"] == '"requests";q=10;w=86400'
        quiet = Setup(Quota("requests", limit=10), headers=False)
        assert "RateLimit" not in quiet.client.get("/search", headers=as_tenant("acme")).headers

    def test_a_new_period_starts_again(self, clock: Callable[[datetime], None]) -> None:
        setup = Setup(Quota("requests", limit=1, period="hour"))
        assert setup.client.get("/search", headers=as_tenant("acme")).status_code == 200
        assert setup.client.get("/search", headers=as_tenant("acme")).status_code == 429
        clock(NOW + timedelta(minutes=30))
        assert setup.client.get("/search", headers=as_tenant("acme")).status_code == 200

    def test_402_for_a_plan_to_upgrade(self) -> None:
        setup = Setup(Quota("requests", limit=0, status=402))
        assert setup.client.get("/search", headers=as_tenant("acme")).status_code == 402

    def test_an_amount_that_doesnt_fit_isnt_charged(self) -> None:
        setup = Setup(Quota("rows", limit=12))
        statuses = [
            setup.client.get("/export", headers=as_tenant("acme")).status_code for _ in range(3)
        ]
        assert statuses == [200, 200, 429]  # 10 used: 5 more don't fit in 12

    @pytest.mark.parametrize("asynchronous", [False, True])
    def test_each_subjects_plan(self, asynchronous: bool) -> None:
        plans = {"acme": 1, "initech": None}  # None: no limit

        def plan(subject: str) -> int | None:
            return plans[subject]

        async def plan_from_the_database(subject: str) -> int | None:
            return plans[subject]

        setup = Setup(Quota("requests", limit=plan_from_the_database if asynchronous else plan))
        acme = [setup.client.get("/search", headers=as_tenant("acme")) for _ in range(2)]
        assert [response.status_code for response in acme] == [200, 429]
        initech = setup.client.get("/search", headers=as_tenant("initech"))
        assert initech.status_code == 200
        assert "RateLimit" not in initech.headers

    def test_a_limit_that_isnt_a_number(self) -> None:
        setup = Setup(Quota("requests", limit=lambda _: "100"))  # type: ignore[arg-type,return-value]
        with pytest.raises(TypeError, match="number of units or None"):
            TestClient(setup.app).get("/search", headers=as_tenant("acme"))

    async def test_concurrent_requests_never_go_past_it(self) -> None:
        setup = Setup(Quota("requests", limit=5))
        transport = httpx.ASGITransport(app=setup.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(client.get("/search", headers=as_tenant("acme")) for _ in range(20))
            )
        assert sorted(response.status_code for response in responses) == [200] * 5 + [429] * 15
        assert await setup.used("acme", "requests") == 5

    def test_problem_details_carry_the_headers(self) -> None:
        setup = Setup(Quota("requests", limit=1))
        FastAPIProblemDetails().install(setup.app)
        setup.client.get("/search", headers=as_tenant("acme"))
        refused = setup.client.get("/search", headers=as_tenant("acme"))
        assert refused.headers["content-type"] == "application/problem+json"
        assert refused.headers["RateLimit"].startswith('"requests";r=0;')
        assert "Retry-After" in refused.headers


class TestMetering:
    async def test_what_the_endpoint_adds_is_charged(self) -> None:
        setup = Setup(Quota("tokens", limit=1000))
        first = setup.client.post("/answers?tokens=300", headers=as_tenant("acme"))
        assert first.json() == {"remaining": 1000}
        assert setup.client.post("/answers?tokens=0", headers=as_tenant("acme")).json() == {
            "remaining": 700
        }
        assert await setup.used("acme", "tokens") == 300
        assert setup.charged() == [("acme", "tokens", 300)]  # nothing for 0

    async def test_the_quota_is_checked_before(self) -> None:
        setup = Setup(Quota("tokens", limit=500))
        statuses = [
            setup.client.post("/answers?tokens=400", headers=as_tenant("acme")).status_code
            for _ in range(3)
        ]
        assert statuses == [200, 200, 429]  # the second one goes past it, then nothing more
        assert await setup.used("acme", "tokens") == 800

    async def test_what_was_used_is_charged_even_if_it_fails(self) -> None:
        setup = Setup()
        assert setup.client.post("/answers/fail", headers=as_tenant("acme")).status_code == 500
        assert await setup.used("acme", "tokens") == 7
        assert setup.charged() == [("acme", "tokens", 7)]

    @pytest.mark.parametrize("amount", [-1, 1.5, True])
    def test_only_whole_units(self, amount: Any) -> None:
        with pytest.raises(ValueError, match="whole number of units"):
            Metering("tokens", "acme", None).add(amount)


class TestOutsideRequests:
    async def test_record_charges_without_the_quota(self) -> None:
        setup = Setup(Quota("tokens", limit=10))
        record = await setup.accounting.record("acme", "tokens", 25)
        assert isinstance(record, UsageRecord)
        assert (record.subject, record.amount, record.route) == ("acme", 25, None)
        report = await setup.accounting.usage("acme", "tokens")
        assert (report.used, report.limit, report.remaining) == (25, 10, 0)
        assert report.resets == datetime(2026, 12, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="amount is a number of units"):
            await setup.accounting.record("acme", "tokens", 0)
        with pytest.raises(ValueError, match="the subject's ID"):
            await setup.accounting.record(None, "tokens", 1)  # type: ignore[arg-type]

    async def test_usage_of_the_period_before(
        self,
        clock: Callable[[datetime], None],
    ) -> None:
        setup = Setup()
        await setup.accounting.record("acme", "requests", 3)
        clock(datetime(2026, 12, 2, tzinfo=UTC))
        await setup.accounting.record("acme", "requests", 1)
        november = await setup.accounting.usage("acme", "requests", at=NOW)
        assert (november.period, november.used, november.limit) == ("2026-11", 3, None)
        assert (await setup.accounting.usage("acme", "requests")).used == 1
        with pytest.raises(ValueError, match="the current period and the one before"):
            await setup.accounting.usage("acme", "requests", at=datetime(2026, 10, 5, tzinfo=UTC))

    async def test_the_default_cache(self) -> None:
        from fastapi_alcazaba import FastAPICache

        FastAPICache.init(InMemoryBackend())
        accounting = FastAPIAccounting(subject=tenant, sinks=())
        await accounting.record("acme", "requests", 2)
        assert (await accounting.usage("acme", "requests")).used == 2


class Down(InMemoryBackend):
    async def incr_window(self, *args: Any, **kwargs: Any) -> tuple[bool, int, int]:
        raise ConnectionError("down")

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        raise ConnectionError("down")

    async def get_counter(self, key: str) -> int:
        raise ConnectionError("down")


class TestFailures:
    def test_a_failing_backend_lets_requests_through_and_records_them(self) -> None:
        setup = Setup(Quota("requests", limit=1), backend=Down())
        statuses = [setup.client.get("/search", headers=as_tenant("acme")) for _ in range(3)]
        assert [response.status_code for response in statuses] == [200] * 3
        assert setup.charged() == [("acme", "requests", 1)] * 3  # billing still has them
        assert setup.client.post("/answers?tokens=9", headers=as_tenant("acme")).status_code == 200
        assert setup.charged()[-1] == ("acme", "tokens", 9)

    def test_or_refuses_them(self) -> None:
        setup = Setup(Quota("requests", limit=1), backend=Down(), fail_open=False)
        assert setup.client.get("/search", headers=as_tenant("acme")).status_code == 503
        assert setup.charged() == []

    def test_a_server_error_uncounted_isnt_refunded(self) -> None:
        """The backend down: nothing was counted, nothing to give back."""
        setup = Setup(backend=Down())
        assert setup.client.get("/fails/500", headers=as_tenant("acme")).status_code == 500
        assert setup.charged() == []

    def test_an_open_circuit_isnt_logged_each_time(self, caplog: pytest.LogCaptureFixture) -> None:
        setup = Setup()
        setup.cache = Cache(Down(), circuit_breaker_failures=1)
        setup.accounting.cache = setup.cache
        with caplog.at_level(logging.DEBUG, logger="fastapi_alcazaba.accounting.meter"):
            for _ in range(2):
                setup.client.get("/search", headers=as_tenant("acme"))
        levels = [r.levelname for r in caplog.records if r.getMessage().startswith("Usage of")]
        assert levels == ["WARNING", "DEBUG"]  # then the circuit is open

    async def test_usage_raises(self) -> None:
        setup = Setup(backend=Down())
        with pytest.raises(BackendUnavailable):
            await setup.accounting.usage("acme", "requests")

    async def test_a_refund_that_fails_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        class NoRefunds(InMemoryBackend):
            async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
                if amount < 0:
                    raise ConnectionError("down")
                return await super().incr(key, amount, expire)

        setup = Setup(backend=NoRefunds())
        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.accounting.meter"):
            setup.client.get("/fails/500", headers=as_tenant("acme"))
        assert "Couldn't refund 1 of requests" in caplog.text
        assert await setup.used("acme", "requests") == 1  # until the period ends

    def test_a_failing_sink_doesnt_keep_the_record_from_the_others(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class Failing:
            async def write(self, record: UsageRecord) -> None:
                raise ConnectionError("the database is down")

        class Slow:
            async def write(self, record: UsageRecord) -> None:
                del record  # never written: it takes too long
                await asyncio.sleep(1)

        kept = MemoryUsageSink()
        reader = InMemoryMetricReader()
        metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
        setup = Setup(sinks=[Failing(), Slow(), kept], timeout=0.05, metrics=metrics)
        with caplog.at_level(logging.ERROR, logger="fastapi_alcazaba.accounting.meter"):
            assert setup.client.get("/search", headers=as_tenant("acme")).status_code == 200
        assert len(kept.records) == 1
        assert "not written by Failing (ConnectionError)" in caplog.text
        assert "not written by Slow (took longer than 0.05 s)" in caplog.text
        assert '"subject":"acme"' in caplog.text  # logged, the record isn't lost
        assert sorted(
            (attributes["fastapi_alcazaba.sink"], attributes["fastapi_alcazaba.reason"])
            for attributes, _ in points(reader, "fastapi_alcazaba.accounting.undelivered")
        ) == [("Failing", "error"), ("Slow", "timeout")]


def test_metrics() -> None:
    reader = InMemoryMetricReader()
    metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
    setup = Setup(Quota("requests", limit=1), metrics=metrics)
    setup.client.get("/search", headers=as_tenant("acme"))
    setup.client.get("/search", headers=as_tenant("acme"))
    setup.client.get("/search")
    setup.client.get("/fails/500", headers=as_tenant("initech"))
    setup.client.post("/answers?tokens=40", headers=as_tenant("acme"))
    outcomes = {
        (attributes["fastapi_alcazaba.meter"], attributes["fastapi_alcazaba.outcome"]): value
        for attributes, value in points(reader, "fastapi_alcazaba.accounting")
    }
    assert outcomes == {
        ("requests", "allowed"): 2,
        ("requests", "exceeded"): 1,
        ("requests", "unauthenticated"): 1,
        ("requests", "refunded"): 1,
        ("tokens", "allowed"): 1,
    }
    units = {
        attributes["fastapi_alcazaba.meter"]: value
        for attributes, value in points(reader, "fastapi_alcazaba.accounting.usage")
    }
    assert units == {"requests": 1, "tokens": 40}


class TestWebSockets:
    def app(self, setup: Setup) -> TestClient:
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(setup.accounting.meter("connections"))])
        async def chat(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text("hello")
            await websocket.close()

        return TestClient(app)

    def test_metered_and_held_to_the_quota(self) -> None:
        setup = Setup(Quota("connections", limit=1))
        client = self.app(setup)
        with client.websocket_connect("/ws", headers=as_tenant("acme")) as websocket:
            assert websocket.receive_text() == "hello"
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/ws", headers=as_tenant("acme")),
        ):
            pass
        assert refused.value.code == 1008

    @pytest.mark.parametrize(("code", "used"), [(1008, 1), (1011, 0)])
    async def test_closed_by_the_endpoint(self, code: int, used: int) -> None:
        """Closed for a policy of its own (1008): served, and charged. An error: refunded."""
        setup = Setup()
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(setup.accounting.meter("connections"))])
        async def chat(websocket: WebSocket) -> None:
            raise WebSocketException(code)

        with (
            pytest.raises(WebSocketDisconnect),
            TestClient(app).websocket_connect("/ws", headers=as_tenant("acme")),
        ):
            pass
        assert await setup.used("acme", "connections") == used

    def test_a_failing_backend_closes_with_try_again_later(self) -> None:
        setup = Setup(backend=Down(), fail_open=False)
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            self.app(setup).websocket_connect("/ws", headers=as_tenant("acme")),
        ):
            pass
        assert refused.value.code == 1013

    def test_without_a_subject(self) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            self.app(Setup()).websocket_connect("/ws"),
        ):
            pass
        assert refused.value.code == 1008


class TestLoggingSink:
    async def test_one_line_of_json(self, caplog: pytest.LogCaptureFixture) -> None:
        setup = Setup(sinks=[LoggingUsageSink()])
        with caplog.at_level(logging.INFO, logger="fastapi_alcazaba.accounting.usage"):
            await setup.accounting.record("acme", "tokens", 3)
        (logged,) = [r for r in caplog.records if r.name == "fastapi_alcazaba.accounting.usage"]
        assert json.loads(logged.getMessage())["amount"] == 3
        assert logged.usage["meter"] == "tokens"  # type: ignore[attr-defined]

    async def test_warns_once_when_records_are_lost(self, caplog: pytest.LogCaptureFixture) -> None:
        log = logging.getLogger("tests.accounting.lost")
        log.propagate = False
        try:
            setup = Setup(sinks=[LoggingUsageSink(log)])
            with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.accounting.records"):
                await setup.accounting.record("acme", "tokens", 1)
                await setup.accounting.record("acme", "tokens", 1)
        finally:
            log.propagate = True
        assert caplog.text.count("Usage records are being lost") == 1


def test_the_backend_never_sees_the_subjects() -> None:
    backend = InMemoryBackend()
    setup = Setup(Quota("requests", limit=5), backend=backend)
    setup.client.get("/search", headers=as_tenant("acme-corporation"))
    keys = list(backend._state["usage"])  # pylint: disable=protected-access
    assert keys
    assert not any("acme" in key for key in keys)
