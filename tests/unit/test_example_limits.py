"""The limits example: rate limits per address and per user, and a concurrency limit."""

import asyncio
import ipaddress
import itertools
import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.limits import main
from examples.limits.main import Report, app

_numbers = itertools.count(1)


def new_address() -> str:
    """An address no other test used. Each test starts with a new backend, but the limiters also
    remember in the process who is over the limit, to refuse them without asking the backend."""
    return str(ipaddress.IPv4Address("10.0.0.0") + next(_numbers))


def from_address(host: str) -> TestClient:
    """Another client of the app, from `host`; the lifespan ran already."""
    return TestClient(app, client=(host, 50000))


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(app, client=(new_address(), 50000)) as client:
        yield client


@pytest.fixture(name="sign_up")
def fixture_sign_up(monkeypatch: pytest.MonkeyPatch) -> Callable[[], dict[str, str]]:
    """Adds a user that no other test used, and returns the headers of their requests."""

    def new_user() -> dict[str, str]:
        n = next(_numbers)
        monkeypatch.setitem(main.TOKENS, f"token-{n}", f"user-{n}")
        return {"Authorization": f"Bearer token-{n}"}

    return new_user


class Renderer:
    """Renders reports at once, or holds them until the test lets them finish."""

    def __init__(self) -> None:
        self.finish = threading.Event()
        self.finish.set()
        self.started = threading.Semaphore(0)
        self.running = self.most = 0

    async def render(self, user: str, month: str) -> Report:
        self.running += 1
        self.most = max(self.most, self.running)
        self.started.release()
        while not self.finish.is_set():
            await asyncio.sleep(0.005)
        self.running -= 1
        return Report(customer=user, month=month, pages=1)

    @contextmanager
    def held(self) -> Iterator[None]:
        self.finish.clear()
        try:
            yield
        finally:
            self.finish.set()

    def wait_started(self, count: int) -> None:
        for _ in range(count):
            # pylint: disable-next=consider-using-with  # a semaphore that counts, not a lock
            assert self.started.acquire(timeout=5)


@pytest.fixture(name="renderer")
def fixture_renderer(monkeypatch: pytest.MonkeyPatch) -> Renderer:
    fake = Renderer()
    monkeypatch.setattr(main, "render_report", fake.render)
    return fake


def report(client: TestClient, user: dict[str, str]) -> httpx.Response:
    return client.post("/reports", json={"month": "2026-09"}, headers=user)


def left(response: httpx.Response, policy: str) -> int:
    """What the RateLimit header says is left."""
    found = re.fullmatch(rf'"{policy}";r=(\d+);t=\d+', response.headers["ratelimit"])
    assert found is not None, response.headers["ratelimit"]
    return int(found[1])


def retry_after(response: httpx.Response) -> int:
    return int(response.headers["retry-after"])


class TestPerAddress:
    def test_ten_searches_a_minute(self, client: TestClient) -> None:
        responses = [client.get("/search?q=oli") for _ in range(10)]
        assert [left(response, "10/60s") for response in responses] == list(range(9, -1, -1))
        assert responses[0].json() == [
            {"name": "Olive oil", "price": 9.5},
            {"name": "Olives", "price": 3.2},
        ]
        assert responses[0].headers["ratelimit-policy"] == '"10/60s";q=10;w=60'
        refused = client.get("/search?q=oli")
        assert refused.status_code == 429
        assert 0 < retry_after(refused) <= 120
        assert left(refused, "10/60s") == 0

    def test_each_address_has_its_own_budget(self, client: TestClient) -> None:
        for _ in range(10):
            client.get("/search?q=oil")
        assert client.get("/search?q=oil").status_code == 429
        assert from_address(new_address()).get("/search?q=oil").status_code == 200

    def test_x_forwarded_for_doesnt_give_a_new_budget(self, client: TestClient) -> None:
        for _ in range(10):
            client.get("/search?q=oil")
        spoofed = client.get("/search?q=oil", headers={"X-Forwarded-For": new_address()})
        assert spoofed.status_code == 429

    @pytest.mark.usefixtures("client")
    def test_ipv6_clients_by_their_64_network(self) -> None:
        network = f"2001:db8:{next(_numbers):x}"
        statuses = [
            from_address(f"{network}::{n:x}").get("/search?q=oil").status_code for n in range(11)
        ]
        assert statuses == [200] * 10 + [429]
        assert from_address(f"{network}:1::1").get("/search?q=oil").status_code == 200


class TestPerUser:
    def test_users_need_a_token(self, client: TestClient) -> None:
        for headers in ({}, {"Authorization": "Bearer made-up"}, {"Authorization": "Basic x"}):
            response = client.get("/orders", headers=headers)
            assert (response.status_code, response.headers["www-authenticate"]) == (401, "Bearer")
            assert "ratelimit" not in response.headers  # refused before the limiter

    @pytest.mark.usefixtures("renderer")
    def test_one_budget_for_orders_and_reports(
        self,
        client: TestClient,
        sign_up: Callable[[], dict[str, str]],
    ) -> None:
        user = sign_up()
        orders = client.get("/orders", headers=user)
        assert (orders.status_code, left(orders, "api")) == (200, 29)
        assert orders.headers["ratelimit-policy"] == '"api";q=30;w=60'
        assert [order["total"] for order in orders.json()] == [12.5, 25.0, 37.5]
        reports = [report(client, user) for _ in range(2)]
        assert [left(response, "api") for response in reports] == [19, 9]
        assert reports[0].json()["month"] == "2026-09"
        statuses = [client.get("/orders", headers=user).status_code for _ in range(10)]
        assert statuses == [200] * 9 + [429]

    def test_a_report_takes_10_of_the_budget(
        self,
        client: TestClient,
        sign_up: Callable[[], dict[str, str]],
        renderer: Renderer,
    ) -> None:
        user = sign_up()
        for _ in range(25):
            client.get("/orders", headers=user)
        refused = report(client, user)
        assert (refused.status_code, left(refused, "api")) == (429, 5)
        assert 0 < retry_after(refused) <= 120
        # Cheaper requests still fit in what's left.
        assert left(client.get("/orders", headers=user), "api") == 4
        assert renderer.most == 0

    @pytest.mark.usefixtures("renderer")
    def test_each_user_has_their_own_budget(
        self,
        client: TestClient,
        sign_up: Callable[[], dict[str, str]],
    ) -> None:
        first, second = sign_up(), sign_up()
        assert [report(client, first).status_code for _ in range(4)] == [200, 200, 200, 429]
        assert client.get("/orders", headers=first).status_code == 429
        assert left(client.get("/orders", headers=second), "api") == 29

    def test_invalid_requests_count_too(
        self,
        client: TestClient,
        sign_up: Callable[[], dict[str, str]],
        renderer: Renderer,
    ) -> None:
        user = sign_up()
        assert client.post("/reports", json={"month": "13"}, headers=user).status_code == 422
        assert left(client.get("/orders", headers=user), "api") == 19
        assert renderer.most == 0


class TestReportSlots:
    def test_two_reports_at_a_time(
        self,
        client: TestClient,
        sign_up: Callable[[], dict[str, str]],
        renderer: Renderer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(main.report_slots, "wait", 0.1)  # not the app's 2 s: a faster test
        users = [sign_up() for _ in range(3)]
        with ThreadPoolExecutor(2) as pool, renderer.held():
            running = [pool.submit(report, client, user) for user in users[:2]]
            renderer.wait_started(2)
            refused = report(client, users[2])
            assert (refused.status_code, retry_after(refused)) == (503, 3)
            assert refused.json() == {"detail": "Service Unavailable"}
            renderer.finish.set()
            assert [future.result(5).status_code for future in running] == [200, 200]
        assert report(client, users[2]).status_code == 200  # the slots were given back
        assert renderer.most == 2

    def test_a_report_waits_for_a_free_slot(
        self,
        client: TestClient,
        sign_up: Callable[[], dict[str, str]],
        renderer: Renderer,
    ) -> None:
        users = [sign_up() for _ in range(3)]
        with ThreadPoolExecutor(3) as pool, renderer.held():
            running = [pool.submit(report, client, user) for user in users[:2]]
            renderer.wait_started(2)
            waiting = pool.submit(report, client, users[2])
            time.sleep(0.3)
            assert not waiting.done()
            assert renderer.running == 2
            renderer.finish.set()
            responses = [future.result(5) for future in [*running, waiting]]
        assert [response.status_code for response in responses] == [200, 200, 200]
        assert renderer.most == 2

    def test_over_the_budget_a_report_doesnt_wait_for_a_slot(
        self,
        client: TestClient,
        sign_up: Callable[[], dict[str, str]],
        renderer: Renderer,
    ) -> None:
        spender, *others = (sign_up() for _ in range(3))
        for _ in range(3):
            report(client, spender)
        with ThreadPoolExecutor(2) as pool, renderer.held():
            running = [pool.submit(report, client, user) for user in others]
            renderer.wait_started(2)
            started = time.monotonic()
            assert report(client, spender).status_code == 429
            assert time.monotonic() - started < 1  # refused at once, not after 2 s of waiting
            renderer.finish.set()
            assert [future.result(5).status_code for future in running] == [200, 200]
