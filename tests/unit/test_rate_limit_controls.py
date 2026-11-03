"""FastAPIRateLimiter's controls: limits computed per request, exemptions, resetting a client and
reading its budget without spending any."""

import logging
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba import Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter, RateLimitStatus, too_many_requests
from tests.unit.test_limiter import Clock, fixture_clock, limited_app
from tests.unit.test_metrics import points

__all__ = ["fixture_clock"]  # the `clock` fixture, from the limiter's tests

PLANS = {"free": 2, "pro": 5}


def plan(request: Any) -> int:
    return PLANS[request.headers.get("x-plan", "free")]


async def plan_async(request: Any) -> int:
    return plan(request)


@pytest.fixture(name="backend", autouse=True)
def fixture_backend() -> InMemoryBackend:
    backend = InMemoryBackend()
    FastAPICache.init(backend)
    return backend


def statuses(client: TestClient, count: int, **headers: str) -> list[int]:
    return [client.get("/", headers=headers).status_code for _ in range(count)]


class TestLimitsPerRequest:
    @pytest.mark.parametrize("times", [plan, plan_async])
    def test_by_plan(self, clock: Clock, times: Any) -> None:
        del clock
        client = limited_app(FastAPIRateLimiter(times=times, minutes=1))
        assert statuses(client, 3) == [200, 200, 429]
        pro = TestClient(client.app, client=("10.0.0.2", 1))
        assert statuses(pro, 6, **{"x-plan": "pro"}) == [200] * 5 + [429]

    def test_the_headers_say_the_request_s_limit(self, clock: Clock) -> None:
        del clock
        client = limited_app(FastAPIRateLimiter(times=plan, minutes=1))
        response = client.get("/", headers={"x-plan": "pro"})
        assert response.headers["ratelimit-policy"] == '"per-60s";q=5;w=60'
        assert response.headers["ratelimit"] == '"per-60s";r=4;t=60'

    def test_a_plan_changed_keeps_what_was_spent(self, clock: Clock) -> None:
        del clock
        client = limited_app(FastAPIRateLimiter(times=plan, minutes=1))
        assert statuses(client, 2) == [200, 200]  # free: all of it
        assert statuses(client, 4, **{"x-plan": "pro"}) == [200, 200, 200, 429]

    @pytest.mark.parametrize("bad", [0, -1, "5", True])
    def test_a_limit_that_isnt_one(self, clock: Clock, bad: Any) -> None:
        del clock
        client = limited_app(FastAPIRateLimiter(times=lambda _: bad, minutes=1))
        with pytest.raises(ValueError, match="int of 1 or more"):
            client.get("/")

    def test_a_cost_over_the_request_s_limit(self, clock: Clock) -> None:
        del clock
        client = limited_app(FastAPIRateLimiter(times=plan, minutes=1, cost=3))
        with pytest.raises(ValueError, match="more than the request's limit"):
            client.get("/")

    def test_invalid(self) -> None:
        with pytest.raises(ValueError, match="cost must be at least 1"):
            FastAPIRateLimiter(times=plan, minutes=1, cost=0)


class TestExemptions:
    def test_identities(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO)
        reader = InMemoryMetricReader()
        FastAPICache.reset()  # init() keeps the first one: the autouse fixture's
        FastAPICache.init(InMemoryBackend(), meter_provider=MeterProvider(metric_readers=[reader]))
        limiter = FastAPIRateLimiter(times=1, minutes=1, exempt={"10.0.0.9"})
        client = limited_app(limiter)
        internal = TestClient(client.app, client=("10.0.0.9", 1))
        assert statuses(internal, 5) == [200] * 5
        assert "ratelimit" not in internal.get("/").headers
        assert statuses(client, 2) == [200, 429]
        assert caplog.text.count("'10.0.0.9' is exempt") == 1  # logged once
        outcomes = {
            a["fastapi_alcazaba.outcome"]: v
            for a, v in points(reader, "fastapi_alcazaba.rate_limit")
        }
        assert outcomes == {"exempt": 6, "allowed": 1, "limited": 1}

    @pytest.mark.parametrize("sync", [True, False])
    def test_a_predicate(self, clock: Clock, sync: bool) -> None:
        del clock

        def admin(request: Any) -> bool:
            return bool(request.headers.get("x-admin") == "yes")

        async def admin_async(request: Any) -> bool:
            return admin(request)

        client = limited_app(
            FastAPIRateLimiter(times=1, minutes=1, exempt=admin if sync else admin_async)
        )
        assert statuses(client, 3, **{"x-admin": "yes"}) == [200] * 3
        assert statuses(client, 2) == [200, 429]

    def test_a_string_isnt_a_list_of_identities(self) -> None:
        with pytest.raises(TypeError, match="not a string"):
            FastAPIRateLimiter(times=1, minutes=1, exempt="10.0.0.9")

    def test_logged_identities_are_bounded(
        self,
        clock: Clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del clock
        monkeypatch.setattr("fastapi_alcazaba.rate_limit.limiter._MAX_LOGGED_EXEMPTIONS", 2)
        limiter = FastAPIRateLimiter(
            times=1, minutes=1, exempt=lambda _: True, identifier=lambda r: r.headers["x-id"]
        )
        client = limited_app(limiter)
        for identity in "abc":
            client.get("/", headers={"x-id": identity})
        assert list(limiter._logged_exemptions) == ["b", "c"]  # pylint: disable=protected-access


def login_app(attempts: FastAPIRateLimiter) -> TestClient:
    app = FastAPI()

    @app.post("/login")
    async def login(request: Request, username: str, password: str) -> dict[str, Any]:
        if (wait := await attempts.hit(request, key=username)) is not None:
            too_many_requests(request, wait)
        if password != "right":  # noqa: S105  # the test's
            return {"ok": False}
        await attempts.reset(request, key=username)  # what the failures counted is forgotten
        return {"ok": True}

    @app.get("/left")
    async def left(request: Request, username: str) -> dict[str, Any]:
        found = await attempts.status(request, key=username)
        return {"limit": found.limit, "remaining": found.remaining, "reset": found.reset}

    return TestClient(app)


class TestResetAndStatus:
    def test_a_successful_login_forgets_the_failures(self, clock: Clock) -> None:
        del clock
        attempts = FastAPIRateLimiter(times=3, minutes=15, identifier=lambda _: "", name="login")
        client = login_app(attempts)

        def log_in(password: str) -> int:
            return client.post(
                "/login", params={"username": "ana", "password": password}
            ).status_code

        assert [log_in("wrong"), log_in("wrong")] == [200, 200]
        assert client.get("/left", params={"username": "ana"}).json() == {
            "limit": 3,
            "remaining": 1,
            "reset": 900,
        }
        assert log_in("right") == 200
        assert client.get("/left", params={"username": "ana"}).json()["remaining"] == 3
        assert [log_in("wrong") for _ in range(4)] == [200, 200, 200, 429]
        left = client.get("/left", params={"username": "ana"}).json()
        assert left["remaining"] == 0
        assert left["reset"] > 900  # the next window, while this one still weighs

    def test_status_spends_nothing(self, clock: Clock) -> None:
        del clock
        limiter = FastAPIRateLimiter(times=2, minutes=1)
        client = limited_app(limiter)

        app = client.app
        assert isinstance(app, FastAPI)

        @app.get("/check")
        async def check(request: Request) -> dict[str, int]:
            found = await limiter.status(request)
            return {"remaining": found.remaining}

        for _ in range(5):
            assert client.get("/check").json() == {"remaining": 2}  # another route's budget
        assert statuses(client, 3) == [200, 200, 429]

    def test_the_sliding_window_counts_in_the_status(self, clock: Clock) -> None:
        limiter = FastAPIRateLimiter(times=4, minutes=1, name="shared")
        client = limited_app(limiter)
        assert statuses(client, 4) == [200] * 4
        clock.now += 90  # half of the previous window still weighs: 2 of 4
        status = _run(limiter.status(identity="testclient", cache=FastAPICache.instance()))
        assert status == RateLimitStatus(4, 2, 30)

    async def test_by_identity_without_a_request(self, clock: Clock) -> None:
        del clock
        cache = Cache(InMemoryBackend())
        limiter = FastAPIRateLimiter(times=2, minutes=1, name="api", cache=cache)
        client = limited_app(limiter)
        assert statuses(client, 3) == [200, 200, 429]
        assert (await limiter.status(identity="testclient")).remaining == 0
        await limiter.reset(identity="testclient")
        assert (await limiter.status(identity="testclient")).remaining == 2
        assert statuses(client, 1) == [200]  # this process's block forgotten too

    async def test_what_it_needs_without_a_request(self) -> None:
        unnamed = FastAPIRateLimiter(times=2, minutes=1)
        with pytest.raises(ValueError, match="give the identity, and the limiter a name"):
            await unnamed.reset(identity="x")
        named = FastAPIRateLimiter(times=2, minutes=1, name="api")
        with pytest.raises(ValueError, match="give the identity"):
            await named.status()
        with pytest.raises(ValueError, match="give the cache"):
            await named.status(identity="x")
        dynamic = FastAPIRateLimiter(times=plan, minutes=1, name="api")
        with pytest.raises(ValueError, match="needs the request"):
            await dynamic.status(identity="x", cache=Cache(InMemoryBackend()))


def _run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


async def test_hit_validates_cost() -> None:
    limiter = FastAPIRateLimiter(times=2, minutes=1)
    with pytest.raises(ValueError, match="at least 1"):
        await limiter.hit(Any, cost=0)  # type: ignore[arg-type]


def test_dependencies_too(clock: Clock) -> None:
    del clock
    limiter = FastAPIRateLimiter(times=1, minutes=1, exempt={"testclient"})
    app = FastAPI()

    @app.get("/")
    async def root(_: Annotated[None, Depends(limiter)]) -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert statuses(client, 3) == [200] * 3
