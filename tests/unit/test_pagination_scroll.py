"""PaginationScroll: a snapshot of the results kept in the cache's backend, read in batches."""

# pylint: disable=protected-access  # tests look at the backend's internals they check

import base64
import itertools
import json
import logging
import time
from collections.abc import Iterator
from typing import Annotated, Any

import fakeredis
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from pydantic_core import PydanticSerializationError

from fastapi_alcazaba import Cache, FastAPICache
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.pagination.cursor import PaginationCursor
from fastapi_alcazaba.pagination.scroll import (
    PaginationScroll,
    ScrollPage,
    ScrollParams,
)
from fastapi_alcazaba.pagination.values import Sealer

SECRET = "s" * 32
SIGNING_KEY = "k" * 32


class Order(BaseModel):
    id: int
    customer: str = Field(alias="Customer")


def orders(count: int) -> list[Order]:
    return [Order(id=n, Customer=f"customer-{n}") for n in range(count)]


def ids(page: ScrollPage[Order]) -> list[int]:
    return [order.id for order in page.items]


class Clock:
    """time.monotonic, which InMemoryBackend's expiry reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1_000.0
        monkeypatch.setattr(time, "monotonic", lambda: self.now)


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture(name="backend", params=["inmemory", "redis"])
def fixture_backend(request: pytest.FixtureRequest) -> Backend:
    if request.param == "inmemory":
        return InMemoryBackend()
    return RedisBackend(fakeredis.FakeAsyncRedis())


@pytest.fixture(name="cache")
def fixture_cache(backend: Backend) -> Cache:
    return Cache(backend, prefix="shop", signing_key=SIGNING_KEY)


@pytest.fixture(name="scroll")
def fixture_scroll(cache: Cache) -> PaginationScroll[Order]:
    return PaginationScroll(Order, SECRET, default_batch_size=10, max_items=100, cache=cache)


def state(backend: InMemoryBackend) -> dict[str, bytes]:
    return {key: value.data for key, value in backend._state.items()}


async def read_all(scroll: PaginationScroll[Order], first: ScrollPage[Order]) -> list[list[int]]:
    batches, page = [ids(first)], first
    while page.scroll_id is not None:
        page = await scroll.next(scroll.params(scroll_id=page.scroll_id))
        batches.append(ids(page))
    return batches


class TestScroll:
    async def test_batch_after_batch(self, scroll: PaginationScroll[Order]) -> None:
        first = await scroll.start(orders(25), scroll.params())
        assert (ids(first), first.total) == (list(range(10)), 25)
        assert await read_all(scroll, first) == [
            list(range(10)),
            list(range(10, 20)),
            list(range(20, 25)),
        ]

    async def test_the_items_come_back_as_their_type(self, scroll: PaginationScroll[Order]) -> None:
        first = await scroll.start(orders(12), scroll.params())
        second = await scroll.next(scroll.params(scroll_id=first.scroll_id))
        assert second.items == [Order(id=10, Customer="customer-10"), orders(12)[11]]
        assert second.model_dump(by_alias=True)["items"][0] == {"id": 10, "Customer": "customer-10"}

    async def test_writes_after_the_start_dont_show(self, scroll: PaginationScroll[Order]) -> None:
        table = orders(25)
        first = await scroll.start(table, scroll.params())
        table.append(Order(id=99, Customer="late"))
        del table[12]
        table[15] = Order(id=15, Customer="changed")
        second = await scroll.next(scroll.params(scroll_id=first.scroll_id))
        assert ids(second) == list(range(10, 20))
        assert second.items[5].customer == "customer-15"
        last = await scroll.next(scroll.params(scroll_id=second.scroll_id))
        assert (ids(last), last.total, last.scroll_id) == (list(range(20, 25)), 25, None)

    async def test_rows_read_as_they_come(self, scroll: PaginationScroll[Order]) -> None:
        rows = (order for order in orders(15))
        first = await scroll.start(rows, scroll.params())
        assert await read_all(scroll, first) == [list(range(10)), list(range(10, 15))]

    async def test_a_retried_request_gets_the_same_batch(
        self,
        scroll: PaginationScroll[Order],
    ) -> None:
        first = await scroll.start(orders(25), scroll.params())
        params = scroll.params(scroll_id=first.scroll_id)
        again = [await scroll.next(params) for _ in range(3)]
        assert [ids(page) for page in again] == [list(range(10, 20))] * 3
        assert len({page.scroll_id for page in again}) == 1

    async def test_each_request_says_its_batch_size(self, scroll: PaginationScroll[Order]) -> None:
        first = await scroll.start(orders(25), scroll.params(batch_size=3))
        second = await scroll.next(scroll.params(scroll_id=first.scroll_id, batch_size=20))
        assert (ids(first), ids(second)) == ([0, 1, 2], list(range(3, 23)))
        last = await scroll.next(scroll.params(scroll_id=second.scroll_id, batch_size=1))
        assert (ids(last), last.scroll_id is None) == ([23], False)
        rest = await scroll.next(scroll.params(scroll_id=first.scroll_id, batch_size=100))
        assert (len(rest.items), rest.scroll_id) == (22, None)

    async def test_one_batch_keeps_nothing(self) -> None:
        backend = InMemoryBackend()
        scroll = PaginationScroll(Order, SECRET, cache=Cache(backend))
        page = await scroll.start(orders(100), scroll.params())
        assert (len(page.items), page.scroll_id, page.total) == (100, None, 100)
        empty = await scroll.start([], scroll.params())
        assert empty.model_dump() == {"items": [], "scroll_id": None, "total": 0}
        assert not backend._state
        assert not backend._store

    async def test_the_global_cache(self) -> None:
        backend = InMemoryBackend()
        scroll = PaginationScroll(Order, SECRET, default_batch_size=5)
        FastAPICache.init(backend)
        first = await scroll.start(orders(8), scroll.params())
        assert len(state(backend)) == 1
        assert ids(await scroll.next(scroll.params(scroll_id=first.scroll_id))) == [5, 6, 7]


class TestLifetime:
    @pytest.fixture(name="backend")
    def fixture_backend(self) -> Backend:
        return InMemoryBackend()

    async def test_the_snapshot_expires(
        self,
        scroll: PaginationScroll[Order],
        clock: Clock,
    ) -> None:
        first = await scroll.start(orders(25), scroll.params())
        clock.now += 61
        with pytest.raises(HTTPException) as gone:
            await scroll.next(scroll.params(scroll_id=first.scroll_id))
        assert (gone.value.status_code, gone.value.detail) == (404, "Expired or unknown scroll_id")

    async def test_each_batch_renews_it(
        self,
        scroll: PaginationScroll[Order],
        clock: Clock,
    ) -> None:
        page = await scroll.start(orders(35), scroll.params())
        scroll_id = page.scroll_id
        for expected in (10, 20, 30):
            clock.now += 50  # 150 seconds in all, more than keep_alive, never 60 without a batch
            scroll_id = page.scroll_id
            page = await scroll.next(scroll.params(scroll_id=scroll_id))
            assert ids(page)[0] == expected
        assert page.scroll_id is None
        clock.now += 50  # a retry of the last batch renews it too
        assert ids(await scroll.next(scroll.params(scroll_id=scroll_id)))[0] == 30
        clock.now += 61
        with pytest.raises(HTTPException) as gone:
            await scroll.next(scroll.params(scroll_id=scroll_id))
        assert gone.value.status_code == 404

    async def test_clear(self, scroll: PaginationScroll[Order], backend: InMemoryBackend) -> None:
        first = await scroll.start(orders(25), scroll.params())
        assert first.scroll_id is not None
        assert await scroll.clear(first.scroll_id)
        assert not state(backend)
        with pytest.raises(HTTPException) as gone:
            await scroll.next(scroll.params(scroll_id=first.scroll_id))
        assert gone.value.status_code == 404
        assert not await scroll.clear(first.scroll_id)

    async def test_the_cache_s_clear_leaves_it(
        self,
        scroll: PaginationScroll[Order],
        cache: Cache,
    ) -> None:
        first = await scroll.start(orders(25), scroll.params())
        await cache.clear()
        assert ids(await scroll.next(scroll.params(scroll_id=first.scroll_id)))[0] == 10

    async def test_in_the_shared_tier(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO)  # InMemoryBackend has no pub/sub: TieredBackend says so
        remote = InMemoryBackend()
        tiered = TieredBackend(remote)
        scroll = PaginationScroll(Order, SECRET, default_batch_size=5, cache=Cache(tiered))
        first = await scroll.start(orders(8), scroll.params())
        await scroll.next(scroll.params(scroll_id=first.scroll_id))
        assert [key.split(":")[0] for key in state(remote)] == ["!scroll"]
        assert not tiered.local._state
        assert not tiered.local._store


class TestMaxItems:
    async def test_more_is_a_value_error(self) -> None:
        scroll = PaginationScroll(Order, SECRET, cache=Cache(InMemoryBackend()), max_items=30)
        assert (await scroll.start(orders(30), scroll.params(batch_size=10))).total == 30
        with pytest.raises(ValueError, match="more than 30 items"):
            await scroll.start(orders(31), scroll.params())

    async def test_reading_one_more_at_most(self) -> None:
        scroll = PaginationScroll(Order, SECRET, cache=Cache(InMemoryBackend()), max_items=30)
        endless = (Order(id=n, Customer="x") for n in itertools.count())
        with pytest.raises(ValueError, match="narrow the query"):
            await scroll.start(endless, scroll.params())
        assert next(endless).id == 31


class TestOwners:
    async def test_a_scroll_id_is_good_for_its_owner_only(
        self,
        scroll: PaginationScroll[Order],
    ) -> None:
        first = await scroll.start(orders(25), scroll.params(), owner="ana")
        assert first.scroll_id is not None
        params = scroll.params(scroll_id=first.scroll_id)
        for someone_else in ("ben", None, ""):
            with pytest.raises(HTTPException) as refused:
                await scroll.next(params, owner=someone_else)
            assert refused.value.status_code == 404
            assert not await scroll.clear(first.scroll_id, owner=someone_else)
        assert ids(await scroll.next(params, owner="ana"))[0] == 10
        assert await scroll.clear(first.scroll_id, owner="ana")


class TestSealed:
    async def test_in_the_backend(self) -> None:
        backend = InMemoryBackend()
        plain = PaginationScroll(Order, SECRET, default_batch_size=2, cache=Cache(backend))
        await plain.start(orders(3), plain.params())
        [stored] = state(backend).values()
        assert stored.split(b"\n") == [
            b'{"id":0,"Customer":"customer-0"}',
            b'{"id":1,"Customer":"customer-1"}',
            b'{"id":2,"Customer":"customer-2"}',
        ]
        encrypted = Cache(InMemoryBackend(), encryption_key="e" * 32)
        hidden = PaginationScroll(Order, SECRET, default_batch_size=2, cache=encrypted)
        first = await hidden.start(orders(3), hidden.params())
        [stored] = state(encrypted.backend).values()  # type: ignore[arg-type]
        assert b"customer" not in stored
        assert ids(await hidden.next(hidden.params(scroll_id=first.scroll_id))) == [2]

    async def test_planted_snapshots_are_ignored(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        backend = InMemoryBackend()
        scroll = PaginationScroll(
            Order,
            SECRET,
            default_batch_size=2,
            cache=Cache(backend, signing_key=SIGNING_KEY),
        )
        first = await scroll.start(orders(3), scroll.params())
        [key] = state(backend)
        await backend.set(key, b'{"id":7,"Customer":"planted"}\n' * 3, 60)
        with pytest.raises(HTTPException) as refused:
            await scroll.next(scroll.params(scroll_id=first.scroll_id))
        assert refused.value.status_code == 404
        assert "isn't sealed by us" in caplog.text

    async def test_another_scroll_s_items_are_ignored(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class Product(BaseModel):
            sku: str

        cache = Cache(InMemoryBackend())
        scroll = PaginationScroll(Order, SECRET, default_batch_size=2, cache=cache)
        products = PaginationScroll(Product, SECRET, default_batch_size=2, cache=cache)
        first = await scroll.start(orders(3), scroll.params())
        with pytest.raises(HTTPException) as refused:
            await products.next(products.params(scroll_id=first.scroll_id))
        assert refused.value.status_code == 404
        assert "its items aren't Product" in caplog.text
        assert ids(await scroll.next(scroll.params(scroll_id=first.scroll_id))) == [2]

    async def test_a_namespace_of_its_own(self) -> None:
        cache = Cache(InMemoryBackend())
        scroll = PaginationScroll(Order, SECRET, default_batch_size=2, cache=cache)
        other = PaginationScroll(Order, SECRET, cache=cache, namespace="reports")
        first = await scroll.start(orders(3), scroll.params())
        with pytest.raises(HTTPException) as refused:
            await other.next(other.params(scroll_id=first.scroll_id))
        assert refused.value.status_code == 404

    async def test_items_of_another_type_are_an_error(
        self,
        scroll: PaginationScroll[Order],
    ) -> None:
        wrong: list[Any] = [{"id": n} for n in range(20)]
        with pytest.raises(PydanticSerializationError):
            await scroll.start(wrong, scroll.params())


def raw(payload: object) -> str:
    """An unsigned scroll_id holding `payload`, as a client could write it."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


SNAPSHOT = "A" * 22
OURS = Sealer(SECRET, b"fastapi_alcazaba.scroll")


class TestInvalidScrollIds:
    @pytest.mark.parametrize(
        "scroll_id",
        [
            "not-a-scroll-id",
            raw([SNAPSHOT, 10]),  # unsigned
            Sealer("o" * 32, b"fastapi_alcazaba.scroll").seal([SNAPSHOT, 10]),  # another secret
            Sealer(SECRET, b"fastapi_alcazaba.cursor").seal([SNAPSHOT, 10]),  # a cursor
            PaginationCursor(secret=SECRET).encode((SNAPSHOT, 10)),
            OURS.seal([SNAPSHOT, 0]),
            OURS.seal([SNAPSHOT, -1]),
            OURS.seal([SNAPSHOT, 101]),  # past max_items
            OURS.seal([SNAPSHOT, True]),
            OURS.seal([SNAPSHOT, 10.0]),
            OURS.seal([SNAPSHOT, "10"]),
            OURS.seal(["short", 10]),
            OURS.seal(["A" * 21 + "=", 10]),
            OURS.seal([SNAPSHOT, 10, 1]),
            OURS.seal({"snapshot": SNAPSHOT, "offset": 10}),
            OURS.seal(None),
            "x" * 1025,
        ],
    )
    def test_are_a_400(self, scroll: PaginationScroll[Order], scroll_id: str) -> None:
        with pytest.raises(HTTPException) as refused:
            scroll.params(scroll_id=scroll_id)
        assert (refused.value.status_code, refused.value.detail) == (400, "Invalid scroll_id")

    async def test_tampered(self, scroll: PaginationScroll[Order]) -> None:
        first = await scroll.start(orders(25), scroll.params())
        assert first.scroll_id is not None
        payload, tag = first.scroll_id.split(".")
        snapshot, _ = json.loads(base64.urlsafe_b64decode(payload + "=="))
        for tampered in (
            f"{raw([snapshot, 20])}.{tag}",  # another batch
            f"{payload}.{tag[:-1]}{'B' if tag[-1] == 'A' else 'A'}",
            payload,
            f"{payload}.{tag}é",
        ):
            with pytest.raises(HTTPException) as refused:
                scroll.params(scroll_id=tampered)
            assert refused.value.status_code == 400
        with pytest.raises(HTTPException):
            await scroll.clear(payload)

    async def test_rotating_the_secret(self, cache: Cache) -> None:
        old = PaginationScroll(Order, "o" * 32, default_batch_size=10, cache=cache)
        first = await old.start(orders(25), old.params())
        rotated = PaginationScroll(Order, ["n" * 32, "o" * 32], default_batch_size=10, cache=cache)
        second = await rotated.next(rotated.params(scroll_id=first.scroll_id))
        assert ids(second)[0] == 10
        assert second.scroll_id is not None
        with pytest.raises(HTTPException):
            old.params(scroll_id=second.scroll_id)  # signed with the new one

    def test_a_snapshot_that_never_was(self, scroll: PaginationScroll[Order]) -> None:
        params = scroll.params(scroll_id=OURS.seal([SNAPSHOT, 10]))
        assert (params.snapshot, params.offset) == (SNAPSHOT, 10)


class TestLimits:
    def test_params(self) -> None:
        scroll = PaginationScroll(Order, SECRET, default_batch_size=20, max_batch_size=50)
        assert scroll.params() == ScrollParams(batch_size=20)
        assert scroll.params(batch_size=50).batch_size == 50
        with pytest.raises(ValueError, match="over 50"):
            scroll.params(batch_size=51)

    @pytest.mark.parametrize(
        ("options", "error"),
        [
            ({"default_batch_size": 0}, "default batch_size"),
            ({"default_batch_size": 1001}, "default batch_size"),
            ({"keep_alive": 0}, "keep_alive"),
            ({"max_items": 0}, "max_items"),
        ],
    )
    def test_options(self, options: dict[str, Any], error: str) -> None:
        with pytest.raises(ValueError, match=error):
            PaginationScroll(Order, SECRET, **options)

    def test_a_secret_is_needed(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            PaginationScroll(Order, "k" * 31)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        for wrong in ({"batch_size": 0}, {"offset": -1}):
            with pytest.raises(ValueError, match="batch_size starts at 1"):
                ScrollParams(**wrong)  # type: ignore[arg-type]
        for half in ({"scroll_id": "x"}, {"snapshot": SNAPSHOT}):
            with pytest.raises(ValueError, match="go together"):
                ScrollParams(**half)  # type: ignore[arg-type]
        params = ScrollParams(batch_size=5)
        with pytest.raises(AttributeError):
            params.batch_size = 6  # type: ignore[misc]
        assert repr(params) == (
            "ScrollParams(batch_size=5, scroll_id=None, snapshot=None, offset=0)"
        )
        assert hash(params) == hash(ScrollParams(batch_size=5))

    async def test_start_and_next_in_their_place(self, scroll: PaginationScroll[Order]) -> None:
        first = await scroll.start(orders(25), scroll.params())
        with pytest.raises(ValueError, match="call next"):
            await scroll.start(orders(25), scroll.params(scroll_id=first.scroll_id))
        with pytest.raises(ValueError, match="call start"):
            await scroll.next(scroll.params())


class Down(InMemoryBackend):
    async def get(self, key: str) -> bytes | None:
        raise ConnectionError("down")

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        raise ConnectionError("down")

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        raise ConnectionError("down")


class NoWrites(InMemoryBackend):
    """Reads work, writes fail: the snapshot can't be renewed."""

    fail = False

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        if self.fail:
            raise ConnectionError("read-only")
        await super().set(key, value, expire)


class TestBackendFailures:
    async def test_are_a_503(self, caplog: pytest.LogCaptureFixture) -> None:
        scroll = PaginationScroll(Order, SECRET, default_batch_size=10, cache=Cache(Down()))
        scroll_id = OURS.seal([SNAPSHOT, 10])
        for call in (
            scroll.start(orders(25), scroll.params()),
            scroll.next(scroll.params(scroll_id=scroll_id)),
            scroll.clear(scroll_id),
        ):
            with pytest.raises(HTTPException) as unavailable:
                await call
            assert unavailable.value.status_code == 503
        assert caplog.text.count("Scroll not served: the backend failed") == 3

    async def test_with_the_circuit_open(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="fastapi_alcazaba.pagination.scroll")
        cache = Cache(Down(), circuit_breaker_failures=1)
        scroll = PaginationScroll(Order, SECRET, default_batch_size=10, cache=cache)
        for _ in range(2):
            with pytest.raises(HTTPException) as unavailable:
                await scroll.start(orders(25), scroll.params())
            assert unavailable.value.status_code == 503
        assert "the circuit breaker is open" in caplog.text

    async def test_a_retry_gets_the_batch_that_failed(self) -> None:
        backend = NoWrites()
        scroll = PaginationScroll(Order, SECRET, default_batch_size=10, cache=Cache(backend))
        first = await scroll.start(orders(25), scroll.params())
        params = scroll.params(scroll_id=first.scroll_id)
        backend.fail = True
        with pytest.raises(HTTPException) as unavailable:
            await scroll.next(params)
        assert unavailable.value.status_code == 503
        backend.fail = False
        assert ids(await scroll.next(params)) == list(range(10, 20))


app_scroll = PaginationScroll(
    Order,
    SECRET,
    default_batch_size=4,
    max_batch_size=20,
    max_items=50,
)


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="client")
def fixture_client(calls: list[str]) -> Iterator[TestClient]:
    FastAPICache.init(InMemoryBackend())
    app = FastAPI()

    @app.get("/orders/export")
    async def export(
        params: Annotated[ScrollParams, Depends(app_scroll)],
        customer: str | None = None,
    ) -> ScrollPage[Order]:
        calls.append("export")
        if params.scroll_id is None:
            found = [o for o in orders(10) if customer is None or o.customer == customer]
            return await app_scroll.start(found, params)
        return await app_scroll.next(params)

    @app.delete("/orders/export", status_code=204)
    async def stop(scroll_id: str) -> None:
        await app_scroll.clear(scroll_id)

    with TestClient(app) as client:
        yield client


def page_ids(body: dict[str, Any]) -> list[int]:
    return [order["id"] for order in body["items"]]


class TestWithAnApp:
    def test_following_the_links(self, client: TestClient) -> None:
        response = client.get("/orders/export?batch_size=3")
        seen = page_ids(response.json())
        while "next" in response.links:
            scroll_id = response.json()["scroll_id"]
            url = response.links["next"]["url"]
            assert url == f"/orders/export?batch_size=3&scroll_id={scroll_id}"
            response = client.get(url)
            seen += page_ids(response.json())
        assert seen == list(range(10))
        assert (response.json()["scroll_id"], "link" in response.headers) == (None, False)

    def test_the_batch_as_clients_see_it(self, client: TestClient) -> None:
        body = client.get("/orders/export").json()
        assert body["items"][0] == {"id": 0, "Customer": "customer-0"}
        assert (len(body["items"]), body["total"]) == (4, 10)

    def test_the_other_parameters_are_kept(self, client: TestClient) -> None:
        response = client.get("/orders/export?customer=customer-3")
        assert (page_ids(response.json()), response.json()["scroll_id"]) == ([3], None)
        first = client.get("/orders/export?customer=x&batch_size=1&ref=mail")
        assert first.links == {}
        many = client.get("/orders/export?ref=mail&batch_size=2")
        assert many.links["next"]["url"].startswith("/orders/export?ref=mail&batch_size=2&")

    def test_clear(self, client: TestClient) -> None:
        scroll_id = client.get("/orders/export").json()["scroll_id"]
        assert client.delete(f"/orders/export?scroll_id={scroll_id}").status_code == 204
        gone = client.get(f"/orders/export?scroll_id={scroll_id}")
        assert (gone.status_code, gone.json()) == (404, {"detail": "Expired or unknown scroll_id"})

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/orders/export?batch_size=0", 422),
            ("/orders/export?batch_size=21", 422),  # this scroll's maximum
            ("/orders/export?batch_size=lots", 422),
            ("/orders/export?scroll_id=has%20spaces", 422),
            ("/orders/export?scroll_id=a.b.c", 422),
            (f"/orders/export?scroll_id={'x' * 2000}", 422),
            (f"/orders/export?scroll_id={raw([SNAPSHOT, 4])}", 400),  # not signed
        ],
    )
    def test_wrong_parameters_never_reach_the_endpoint(
        self,
        client: TestClient,
        calls: list[str],
        path: str,
        status: int,
    ) -> None:
        assert client.get(path).status_code == status
        assert calls == []

    def test_the_parameters_are_documented(self, client: TestClient) -> None:
        operation = client.get("/openapi.json").json()["paths"]["/orders/export"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"scroll_id", "batch_size", "customer"}
        size = parameters["batch_size"]["schema"]
        assert (size["maximum"], size["default"]) == (20, 4)
