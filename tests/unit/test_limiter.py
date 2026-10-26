# pylint: disable=protected-access  # tests look at the internals they check

import contextlib
import time
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from hypothesis import assume, given
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.requests import HTTPConnection, Request
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.limiter import FastAPIRateLimiter, _wait, client_ip, client_network
from fastapi_alcazaba.problems import FastAPIProblemDetails
from fastapi_alcazaba.types import Backend
from tests.unit.test_metrics import points

# Unix time (in seconds) at which every window of these tests starts: a whole number of hours.
T0 = 3_600_000.0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(time, "time", clock)
    return clock


class AddAndTakeBack(InMemoryBackend):
    """Without an atomic incr_window, like memcached: Backend's default."""

    incr_window = Backend.incr_window


class Counting(InMemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def incr_window(self, *args: Any, **kwargs: Any) -> tuple[bool, int, int]:
        self.calls += 1
        return await super().incr_window(*args, **kwargs)


@pytest.fixture(
    name="backend", params=[InMemoryBackend, AddAndTakeBack], ids=["atomic", "add-and-take-back"]
)
def fixture_backend(request: pytest.FixtureRequest) -> InMemoryBackend:
    backend: InMemoryBackend = request.param()
    FastAPICache.init(backend)
    return backend


async def ok() -> dict[str, bool]:
    return {"ok": True}


def limited_app(*limiters: FastAPIRateLimiter, path: str = "/") -> TestClient:
    app = FastAPI()
    app.get(path, dependencies=[Depends(limiter) for limiter in limiters])(ok)
    return TestClient(app)


def statuses(client: TestClient, path: str = "/", n: int = 1, **kwargs: Any) -> list[int]:
    return [client.get(path, **kwargs).status_code for _ in range(n)]


def counters(backend: InMemoryBackend) -> list[int]:
    return sorted(int(value.data) for key, value in backend._state.items() if "!ratelimit:" in key)


@pytest.mark.usefixtures("backend")
class TestLimit:
    def test_over_the_limit_a_429_says_when_to_retry(self, clock: Clock) -> None:
        client = limited_app(FastAPIRateLimiter(times=2, seconds=60))
        clock.now += 15
        assert statuses(client, n=2) == [200, 200]
        limited = client.get("/")
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too Many Requests"}
        # In the next window this one weighs 2 × (60 - s) / 60: one more request fits at s = 30.
        assert limited.headers["retry-after"] == "75"
        clock.now += 74
        assert statuses(client) == [429]
        clock.now += 1
        assert statuses(client) == [200]

    @pytest.mark.usefixtures("clock")
    def test_requests_over_the_limit_dont_count(self, backend: InMemoryBackend) -> None:
        client = limited_app(FastAPIRateLimiter(times=2, minutes=1))
        assert statuses(client, n=7) == [200, 200, 429, 429, 429, 429, 429]
        assert counters(backend) == [2]

    def test_the_window_slides(self, clock: Clock) -> None:
        """The previous window counts in proportion to how much of it is less than a window ago:
        no burst of twice the limit around the boundary."""
        client = limited_app(FastAPIRateLimiter(times=10, minutes=1))
        clock.now += 59
        assert statuses(client, n=11).count(200) == 10
        clock.now += 1  # a new window, but the previous one still weighs 10
        assert statuses(client) == [429]
        clock.now += 30  # it weighs 5 now
        assert statuses(client, n=6) == [200] * 5 + [429]
        clock.now += 60  # 5 left in the previous window, which weighs half
        assert statuses(client, n=8).count(200) == 7

    def test_windows_shorter_than_a_second(self, clock: Clock) -> None:
        client = limited_app(FastAPIRateLimiter(times=1, milliseconds=100))
        assert statuses(client, n=2) == [200, 429]
        assert client.get("/").headers["retry-after"] == "1"
        clock.now += 0.2
        assert statuses(client) == [200]


@pytest.mark.usefixtures("backend", "clock")
class TestWhoAndWhere:
    def test_path_parameters_share_the_budget(self) -> None:
        """fastapi-limiter counted each URL apart: /items/1, /items/2... had a budget each."""
        client = limited_app(FastAPIRateLimiter(times=2, minutes=1), path="/items/{item_id}")
        assert [client.get(f"/items/{i}").status_code for i in range(3)] == [200, 200, 429]

    def test_x_forwarded_for_doesnt_change_who_you_are(self) -> None:
        """fastapi-limiter read the client's address from X-Forwarded-For: changing it gave a new
        budget on every request."""
        client = limited_app(FastAPIRateLimiter(times=1, minutes=1))
        assert statuses(client) == [200]
        for address in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            assert statuses(client, headers={"X-Forwarded-For": address}) == [429]

    def test_the_address_comes_from_the_server(self) -> None:
        connection = HTTPConnection({"type": "http", "client": ("203.0.113.7", 4321)})
        assert client_ip(connection) == "203.0.113.7"
        assert client_ip(HTTPConnection({"type": "http", "client": None})) == ""

    def test_ipv6_clients_by_their_network(self) -> None:
        """An ISP gives each customer a /64: one budget per address would be 2⁶⁴ budgets."""
        assert ip("2001:db8:1:2:aaaa::1") == ip("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
        assert ip("2001:db8:1:3::1") == "2001:db8:1:3::/64"
        assert ip("::ffff:203.0.113.7") == "203.0.113.7"
        assert ip("testclient") == "testclient"
        app = FastAPI()
        app.get("/", dependencies=[Depends(FastAPIRateLimiter(times=1, minutes=1))])(ok)
        clients = [TestClient(app, client=(host, 1)) for host in ("2001:db8::1", "2001:db8::2")]
        assert [statuses(client) for client in clients] == [[200], [429]]
        assert statuses(TestClient(app, client=("2001:db8:0:1::1", 1))) == [200]

    def test_other_network_sizes(self) -> None:
        identify = client_network(24, 48)
        connection = HTTPConnection({"type": "http", "client": ("203.0.113.7", 1)})
        assert identify(connection) == "203.0.113.0/24"
        connection = HTTPConnection({"type": "http", "client": ("2001:db8:1:2::1", 1)})
        assert identify(connection) == "2001:db8:1::/48"
        assert identify(HTTPConnection({"type": "http", "client": None})) == ""
        with pytest.raises(ValueError, match="prefixes"):
            client_network(33)

    @pytest.mark.parametrize("asynchronous", [False, True])
    def test_custom_identifier(self, asynchronous: bool) -> None:
        def user(connection: HTTPConnection) -> str:
            return connection.headers.get("x-user", "")

        async def async_user(connection: HTTPConnection) -> str:
            return user(connection)

        client = limited_app(
            FastAPIRateLimiter(times=1, minutes=1, identifier=async_user if asynchronous else user)
        )
        assert statuses(client, n=2, headers={"X-User": "alice"}) == [200, 429]
        assert statuses(client, headers={"X-User": "bob"}) == [200]

    def test_each_route_has_its_own_budget_unless_named(self) -> None:
        app = FastAPI()
        for path in ("/a", "/b"):
            app.get(path, dependencies=[Depends(FastAPIRateLimiter(times=1, minutes=1))])(ok)
        for path in ("/c", "/d"):
            app.get(path, dependencies=[Depends(FastAPIRateLimiter(1, minutes=1, name="search"))])(
                ok
            )
        client = TestClient(app)
        assert [client.get(path).status_code for path in ("/a", "/b", "/a")] == [200, 200, 429]
        assert [client.get(path).status_code for path in ("/c", "/d")] == [200, 429]

    def test_methods_of_a_path_are_different_routes(self) -> None:
        app = FastAPI()
        app.get("/x", dependencies=[Depends(FastAPIRateLimiter(times=1, minutes=1))])(ok)
        app.post("/x", dependencies=[Depends(FastAPIRateLimiter(times=1, minutes=1))])(ok)
        client = TestClient(app)
        assert [client.get("/x").status_code, client.post("/x").status_code] == [200, 200]
        assert [client.get("/x").status_code, client.post("/x").status_code] == [429, 429]

    def test_mounted_apps_have_their_own_budget(self) -> None:
        app = FastAPI()
        for version in ("/v1", "/v2"):
            sub = FastAPI()
            sub.get("/items", dependencies=[Depends(FastAPIRateLimiter(times=1, minutes=1))])(ok)
            app.mount(version, sub)
        client = TestClient(app)
        assert [client.get(p).status_code for p in ("/v1/items", "/v2/items")] == [200, 200]
        assert statuses(client, "/v1/items") == [429]

    def test_different_rates_count_apart(self) -> None:
        client = limited_app(
            FastAPIRateLimiter(times=1, seconds=1), FastAPIRateLimiter(times=3, minutes=1)
        )
        assert statuses(client, n=2) == [200, 429]


@pytest.mark.usefixtures("clock")
class TestBackend:
    def test_identities_never_reach_the_backend_in_clear(self) -> None:
        backend = InMemoryBackend()
        Cache(backend, prefix="app").install(app := FastAPI())
        app.get("/", dependencies=[Depends(FastAPIRateLimiter(times=1, minutes=1))])(ok)
        TestClient(app).get("/")
        (key,) = backend._state
        assert key.startswith("app!ratelimit:")
        assert "testclient" not in key

    def test_the_cache_given(self) -> None:
        backend = InMemoryBackend()
        client = limited_app(FastAPIRateLimiter(times=1, minutes=1, cache=Cache(backend)))
        assert statuses(client, n=2) == [200, 429]
        assert counters(backend) == [1]

    def test_backends_without_counters(self) -> None:
        class NoCounters(InMemoryBackend):
            supports_counters = False

        FastAPICache.init(NoCounters())
        with pytest.raises(NotImplementedError, match="NoCounters can't keep counters"):
            limited_app(FastAPIRateLimiter(times=1, minutes=1)).get("/")

    @pytest.mark.parametrize(("fail_open", "status"), [(True, 200), (False, 503)])
    def test_a_failing_backend(self, fail_open: bool, status: int) -> None:
        reader = InMemoryMetricReader()
        FastAPICache.init(
            Down(), circuit_breaker_failures=2, meter_provider=MeterProvider([reader])
        )
        client = limited_app(FastAPIRateLimiter(times=1, minutes=1, fail_open=fail_open))
        assert statuses(client, n=4) == [status] * 4  # the last three find the circuit open
        assert points(reader, "fastapi_alcazaba.rate_limit") == [
            ({"fastapi_alcazaba.outcome": "unchecked", "fastapi_alcazaba.limiter": "GET /"}, 4)
        ]

    def test_a_failure_to_take_back(self) -> None:
        class NoDecrements(AddAndTakeBack):
            async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
                if amount < 0:
                    raise ConnectionError("down")
                return await super().incr(key, amount, expire)

        backend = NoDecrements()
        client = limited_app(FastAPIRateLimiter(times=1, minutes=1, cache=Cache(backend)))
        assert statuses(client, n=3) == [200, 429, 429]
        # The second stays counted; the third is refused without asking the backend.
        assert counters(backend) == [2]

    async def test_outside_the_routing(self) -> None:
        limiter = FastAPIRateLimiter(times=1, minutes=1, cache=Cache(InMemoryBackend()))
        request = Request({"type": "http", "path": "/raw", "client": ("1.2.3.4", 1)})
        assert await limiter.hit(request) is None
        assert await limiter.hit(request) == 120
        other = Request({"type": "http", "path": "/other", "client": ("1.2.3.4", 1)})
        assert await limiter.hit(other) is None


@pytest.mark.usefixtures("clock")
def test_metrics() -> None:
    reader = InMemoryMetricReader()
    FastAPICache.init(InMemoryBackend(), meter_provider=MeterProvider([reader]))
    client = limited_app(FastAPIRateLimiter(times=2, minutes=1, name="search"))
    statuses(client, n=3)
    assert sorted(points(reader, "fastapi_alcazaba.rate_limit"), key=repr) == [
        ({"fastapi_alcazaba.outcome": "allowed", "fastapi_alcazaba.limiter": "search"}, 2),
        ({"fastapi_alcazaba.outcome": "limited", "fastapi_alcazaba.limiter": "search"}, 1),
    ]


@pytest.mark.usefixtures("backend", "clock")
class TestReactions:
    def test_a_callback_that_returns_lets_the_request_through(self) -> None:
        waits: list[float] = []
        client = limited_app(
            FastAPIRateLimiter(times=1, minutes=1, callback=lambda _, w: waits.append(w))
        )
        assert statuses(client, n=2) == [200, 200]
        assert waits == [120.0]

    def test_an_async_callback(self) -> None:
        async def teapot(connection: HTTPConnection, wait: float) -> None:
            raise HTTPException(418, f"wait {wait:.0f} s")

        client = limited_app(FastAPIRateLimiter(times=1, minutes=1, callback=teapot))
        client.get("/")
        refused = client.get("/")
        assert (refused.status_code, refused.json()) == (418, {"detail": "wait 120 s"})

    def test_limiting_something_else_than_requests(self) -> None:
        """E.g. the login attempts to each account, whoever makes them."""
        attempts = FastAPIRateLimiter(times=2, minutes=15, identifier=lambda _: "")
        app = FastAPI()

        @app.post("/login")
        async def login(request: Request, user: str) -> dict[str, str]:
            if await attempts.hit(request, key=user) is not None:
                raise HTTPException(429)
            return {"user": user}

        client = TestClient(app)
        codes = [client.post(f"/login?user={user}").status_code for user in "aaab"]
        assert codes == [200, 200, 429, 200]


@pytest.mark.usefixtures("backend", "clock")
class TestWebSockets:
    def test_connections(self) -> None:
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(FastAPIRateLimiter(times=1, minutes=1))])
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text("hello")
            await websocket.close()

        client = TestClient(app)
        with client.websocket_connect("/ws") as connection:
            assert connection.receive_text() == "hello"
        with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect("/ws"):
            pass
        assert refused.value.code == 1008

    def test_messages(self) -> None:
        messages = FastAPIRateLimiter(times=2, minutes=1)
        app = FastAPI()

        @app.websocket("/chat")
        async def chat(websocket: WebSocket) -> None:
            await websocket.accept()
            with contextlib.suppress(WebSocketDisconnect):
                while True:
                    text = await websocket.receive_text()
                    over = await messages.hit(websocket, key="message")
                    await websocket.send_text(text if over is None else "slow down")

        with TestClient(app).websocket_connect("/chat") as connection:
            replies = []
            for text in ("a", "b", "c"):
                connection.send_text(text)
                replies.append(connection.receive_text())
        assert replies == ["a", "b", "slow down"]

    def test_a_failing_backend_closes_with_try_again_later(self) -> None:
        limiter = FastAPIRateLimiter(times=1, minutes=1, cache=Cache(Down()), fail_open=False)
        app = FastAPI()
        app.websocket("/ws", dependencies=[Depends(limiter)])(_accept)
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            TestClient(app).websocket_connect("/ws"),
        ):
            pass
        assert refused.value.code == 1013


async def _accept(websocket: WebSocket) -> None:
    await websocket.accept()


class Down(InMemoryBackend):
    async def incr_window(self, *args: Any, **kwargs: Any) -> tuple[bool, int, int]:
        raise ConnectionError("down")


def ip(host: str) -> str:
    return client_ip(HTTPConnection({"type": "http", "client": (host, 1)}))


def request_from(host: str = "192.0.2.1", path: str = "/") -> Request:
    return Request({"type": "http", "path": path, "client": (host, 1)})


@pytest.mark.usefixtures("backend", "clock")
class TestHeaders:
    """The RateLimit headers of draft-ietf-httpapi-ratelimit-headers."""

    def test_what_is_left(self) -> None:
        client = limited_app(FastAPIRateLimiter(times=2, minutes=1))
        response = client.get("/")
        assert response.headers["ratelimit-policy"] == '"2/60s";q=2;w=60'
        assert response.headers["ratelimit"] == '"2/60s";r=1;t=60'
        # Nothing left: t is when one more fits.
        assert client.get("/").headers["ratelimit"] == '"2/60s";r=0;t=90'

    def test_refused_requests(self) -> None:
        client = limited_app(FastAPIRateLimiter(times=1, seconds=10))
        client.get("/")
        for _ in range(2):  # the backend's answer, then this process's
            refused = client.get("/")
            assert refused.status_code == 429
            assert refused.headers["retry-after"] == "20"
            assert refused.headers["ratelimit"] == '"1/10s";r=0;t=20'
            assert refused.headers["ratelimit-policy"] == '"1/10s";q=1;w=10'

    @pytest.mark.parametrize("problems", [True, False])
    def test_error_responses(self, problems: bool) -> None:
        """A 422 or an HTTPException after the limiter: FastAPI builds those from the exception,
        without the dependencies' headers; FastAPIProblemDetails adds the limiter's back."""
        app = FastAPI()

        @app.get("/items/{item_id}", dependencies=[Depends(FastAPIRateLimiter(times=5, minutes=1))])
        async def item(item_id: int) -> int:
            raise HTTPException(503, "Busy")

        if problems:
            FastAPIProblemDetails().install(app)
        client = TestClient(app)
        for path, status in (("/items/not-a-number", 422), ("/items/1", 503)):
            response = client.get(path)
            assert response.status_code == status
            assert ("ratelimit" in response.headers) is problems

    def test_named_policies_and_windows_of_milliseconds(self) -> None:
        client = limited_app(FastAPIRateLimiter(3, milliseconds=1500, name='search "beta"'))
        assert client.get("/").headers["ratelimit-policy"] == r'"search \"beta\"";q=3'

    def test_each_limiter_of_a_route_adds_its_policy(self) -> None:
        client = limited_app(
            FastAPIRateLimiter(times=10, seconds=1), FastAPIRateLimiter(times=100, hours=1)
        )
        response = client.get("/")
        assert response.headers["ratelimit-policy"] == '"10/1s";q=10;w=1, "100/3600s";q=100;w=3600'
        assert response.headers["ratelimit"] == '"10/1s";r=9;t=1, "100/3600s";r=99;t=3600'

    def test_hits_of_cached_endpoints(self) -> None:
        app = FastAPI()

        @app.get("/", dependencies=[Depends(FastAPIRateLimiter(times=5, minutes=1))])
        @cache(expire=60)
        async def cached() -> dict[str, int]:
            return {"n": 1}

        client = TestClient(app)
        client.get("/")
        hit = client.get("/")
        assert (hit.headers["x-fastapi-cache"], hit.headers["ratelimit"]) == (
            "HIT",
            '"5/60s";r=3;t=60',
        )

    def test_a_callback_that_lets_the_request_through(self) -> None:
        client = limited_app(FastAPIRateLimiter(times=1, minutes=1, callback=lambda _, wait: None))
        client.get("/")
        through = client.get("/")
        assert (through.status_code, through.headers["ratelimit"]) == (200, '"1/60s";r=0;t=120')

    def test_turned_off(self) -> None:
        client = limited_app(FastAPIRateLimiter(times=1, minutes=1, headers=False))
        refused = [client.get("/") for _ in range(2)][-1]
        assert "ratelimit" not in client.get("/").headers
        assert ("ratelimit" in refused.headers, refused.headers["retry-after"]) == (False, "120")

    def test_names_must_fit_in_a_header(self) -> None:
        with pytest.raises(ValueError, match="printable ASCII"):
            FastAPIRateLimiter(times=1, minutes=1, name="búsqueda")
        assert (
            FastAPIRateLimiter(times=1, minutes=1, name="búsqueda", headers=False).name
            == "búsqueda"
        )


@pytest.mark.usefixtures("backend", "clock")
class TestCost:
    def test_expensive_routes_take_more_of_a_shared_budget(self) -> None:
        app = FastAPI()
        app.get("/search", dependencies=[Depends(FastAPIRateLimiter(10, minutes=1, name="api"))])(
            ok
        )
        export = FastAPIRateLimiter(10, minutes=1, name="api", cost=4)
        app.get("/export", dependencies=[Depends(export)])(ok)
        client = TestClient(app)
        assert statuses(client, "/export", n=3) == [200, 200, 429]  # 4 more don't fit in 2
        assert client.get("/search").headers["ratelimit"] == '"api";r=1;t=60'
        assert statuses(client, "/search", n=2) == [200, 429]

    async def test_hit_with_a_cost(self) -> None:
        limiter = FastAPIRateLimiter(times=5, minutes=1, cache=Cache(InMemoryBackend()))
        assert await limiter.hit(request_from(), cost=5) is None
        assert await limiter.hit(request_from()) == 72  # 5 × (60 - s) / 60 ≤ 4 at s = 12


@pytest.mark.usefixtures("clock")
class TestFloods:
    def test_clients_with_nothing_left_are_refused_without_asking_the_backend(
        self,
        clock: Clock,
    ) -> None:
        backend = Counting()
        client = limited_app(FastAPIRateLimiter(times=2, minutes=1, cache=Cache(backend)))
        assert statuses(client, n=10) == [200, 200] + [429] * 8
        assert backend.calls == 3
        clock.now += 89
        refused = client.get("/")
        assert (refused.status_code, refused.headers["retry-after"]) == (429, "1")
        clock.now += 1
        assert statuses(client) == [200]
        assert backend.calls == 4

    def test_other_clients_go_on(self) -> None:
        backend = Counting()
        app = FastAPI()
        app.get(
            "/", dependencies=[Depends(FastAPIRateLimiter(1, minutes=1, cache=Cache(backend)))]
        )(ok)
        assert statuses(TestClient(app, client=("192.0.2.1", 1)), n=3) == [200, 429, 429]
        assert statuses(TestClient(app, client=("192.0.2.2", 1))) == [200]
        assert backend.calls == 3

    async def test_while_something_fits_the_backend_decides(self) -> None:
        """A request too expensive for what's left doesn't hold back cheaper ones."""
        backend = Counting()
        limiter = FastAPIRateLimiter(times=10, minutes=1, cache=Cache(backend))
        assert await limiter.hit(request_from(), cost=8) is None
        assert await limiter.hit(request_from(), cost=5) is not None
        assert await limiter.hit(request_from(), cost=2) is None
        assert backend.calls == 3

    async def test_refusals_without_the_backend_wait_for_their_cost(self, clock: Clock) -> None:
        backend = Counting()
        limiter = FastAPIRateLimiter(times=100, minutes=1, cost=50, cache=Cache(backend))
        assert [await limiter.hit(request_from()) for _ in range(2)] == [None, None]
        assert await limiter.hit(request_from()) == pytest.approx(90)  # from the backend
        assert await limiter.hit(request_from()) == pytest.approx(90)  # without it
        assert await limiter.hit(request_from(), cost=1) == pytest.approx(60.6)
        assert backend.calls == 3
        clock.now += 61  # in the next window, where nothing was counted yet
        assert await limiter.hit(request_from()) == pytest.approx(29)
        assert backend.calls == 4

    async def test_a_block_into_the_next_window(self, clock: Clock) -> None:
        limiter = FastAPIRateLimiter(times=10, minutes=1, cache=Cache(Counting()))
        assert await limiter.hit(request_from(), cost=10) is None
        clock.now += 59
        assert await limiter.hit(request_from(), cost=1) == pytest.approx(7)
        clock.now += 1  # the full window is the previous one now
        assert await limiter.hit(request_from(), cost=5) == pytest.approx(30)
        clock.now -= 70  # stepped back to the window before: only the block's end is known
        assert await limiter.hit(request_from(), cost=5) == pytest.approx(76)
        clock.now -= 60  # further: the block would last longer than two windows
        assert await limiter.hit(request_from(), cost=5) is None

    async def test_forget_asks_the_backend_again(self) -> None:
        backend = Counting()
        limiter = FastAPIRateLimiter(times=1, minutes=1, cache=Cache(backend))
        assert await limiter.hit(request_from()) is None
        assert await limiter.hit(request_from()) is not None  # from the backend
        assert await limiter.hit(request_from()) is not None  # without it
        assert backend.calls == 2
        limiter.forget()
        assert await limiter.hit(request_from()) is not None  # still none left in the backend
        assert backend.calls == 3

    def test_a_new_fastapi_cache_forgets_the_last_ones_refusals(self) -> None:
        """As each test's lifespan sets FastAPICache up again: a fresh backend, a fresh budget."""
        limiter = FastAPIRateLimiter(times=1, minutes=1)
        for _ in range(2):
            FastAPICache.reset()
            FastAPICache.init(InMemoryBackend())
            assert statuses(limited_app(limiter), n=2) == [200, 429]

    async def test_a_bounded_number_of_clients(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fastapi_alcazaba.limiter._MAX_BLOCKED", 2)
        limiter = FastAPIRateLimiter(times=1, minutes=1, cache=Cache(InMemoryBackend()))
        for n in range(4):
            assert await limiter.hit(request_from(f"10.0.0.{n}")) is None
            assert await limiter.hit(request_from(f"10.0.0.{n}")) is not None
        assert len(limiter._blocked) == 2


class TestValidation:
    def test_times(self) -> None:
        with pytest.raises(ValueError, match="times"):
            FastAPIRateLimiter(times=0, seconds=1)

    def test_window(self) -> None:
        with pytest.raises(ValueError, match="window"):
            FastAPIRateLimiter(times=1)

    def test_the_window_adds_its_parts(self) -> None:
        assert FastAPIRateLimiter(1, 500, 1, 1, 1).window == 500 + 1000 + 60_000 + 3_600_000

    @pytest.mark.parametrize("cost", [0, 4])
    async def test_costs_fit_in_the_budget(self, cost: int) -> None:
        with pytest.raises(ValueError, match="cost"):
            FastAPIRateLimiter(times=3, minutes=1, cost=cost)
        limiter = FastAPIRateLimiter(times=3, minutes=1, cache=Cache(InMemoryBackend()))
        with pytest.raises(ValueError, match="cost"):
            await limiter.hit(request_from(), cost=cost)


class TestRetryAfter:
    @staticmethod
    def allowed(
        times: int,
        window: float,
        elapsed: float,
        previous: int,
        current: int,
        t: float,
        cost: int,
    ) -> bool:
        """Whether `cost` more fits `t` from now, with no other requests meanwhile."""
        at = elapsed + t
        if at < window:
            estimate = previous * (window - at) / window + current
        elif at < 2 * window:
            estimate = current * (2 * window - at) / window
        else:
            estimate = 0
        return estimate + cost <= times + 1e-9

    @given(
        times=st.integers(1, 100),
        window=st.integers(1, 10**7),
        part=st.floats(0, 1, exclude_max=True),
        previous=st.integers(0, 1_000),
        current=st.integers(0, 1_000),
        cost=st.integers(1, 100),
    )
    def test_waiting_that_long_is_enough_and_not_more(
        self,
        times: int,
        window: int,
        part: float,
        previous: int,
        current: int,
        cost: int,
    ) -> None:
        cost = min(cost, times)
        elapsed = part * window
        state = (times, window, elapsed, previous, current)
        assume(not self.allowed(*state, 0, cost))
        wait = _wait(*state, cost)
        assert self.allowed(*state, wait, cost)
        earlier = wait - window / 1000
        assert earlier <= 0 or not self.allowed(*state, earlier, cost)

    def test_counts_read_at_different_moments(self) -> None:
        """Concurrent requests may leave counts that wouldn't have refused this one."""
        assert _wait(times=2, window=1000, elapsed=0, previous=0, current=0) == 0


@pytest.mark.usefixtures("backend", "clock")
def test_annotated_dependency_style() -> None:
    """FastAPIRateLimiter can also be a parameter: it returns None."""
    app = FastAPI()
    limiter = FastAPIRateLimiter(times=1, minutes=1)

    @app.get("/")
    async def endpoint(_: Annotated[None, Depends(limiter)]) -> dict[str, bool]:
        return {"ok": True}

    assert statuses(TestClient(app), n=2) == [200, 429]
