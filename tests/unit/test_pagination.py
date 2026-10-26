"""Alcazaba's pagination: FastAPIPagination, FastAPIPaginationLimitOffset and
FastAPIPaginationCursor, whose page parameters the cache keeps apart, page by page."""

import base64
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import (
    FastAPICache,
    FastAPIPagination,
    FastAPIPaginationCursor,
    FastAPIPaginationLimitOffset,
)
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.pagination import (
    CursorPage,
    CursorParams,
    LimitOffsetPage,
    LimitOffsetParams,
    Page,
    PageParams,
    Position,
)


class Item(BaseModel):
    id: int


ITEMS = [Item(id=i) for i in range(25)]


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


class TestByPage:
    def test_pages_of_a_list(self) -> None:
        pages = FastAPIPagination()
        page = pages.paginate(ITEMS, pages.params(page=3, size=10))
        assert [item.id for item in page.items] == [20, 21, 22, 23, 24]
        assert (page.total, page.page, page.size, page.pages) == (25, 3, 10, 3)

    def test_past_the_last_page(self) -> None:
        pages = FastAPIPagination()
        assert pages.paginate(ITEMS, pages.params(page=9, size=10)).items == []
        assert pages.paginate([], pages.params()).model_dump() == {
            "items": [],
            "total": 0,
            "page": 1,
            "size": 50,
            "pages": 0,
        }

    def test_a_page_queried_from_a_database(self) -> None:
        pages = FastAPIPagination()
        params = pages.params(page=2, size=10)
        assert (params.offset, params.limit) == (10, 10)
        rows = ITEMS[params.offset : params.offset + params.limit]
        page = pages.paginate(rows, params, total=245)
        assert (len(page.items), page.total, page.pages) == (10, 245, 25)
        with pytest.raises(ValueError, match="negative"):
            pages.paginate(rows, params, total=-1)

    def test_limits(self) -> None:
        pages = FastAPIPagination(default_size=20, max_size=500)
        assert pages.params() == PageParams(page=1, size=20)
        assert pages.params(size=500).limit == 500
        with pytest.raises(ValueError, match="over 500"):
            pages.params(size=501)
        for wrong in ({"default_size": 0}, {"default_size": 101}):
            with pytest.raises(ValueError, match="default size"):
                FastAPIPagination(**wrong)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        for wrong in ({"page": 0}, {"size": 0}):
            with pytest.raises(ValueError, match="start at 1"):
                PageParams(**wrong)
        params = PageParams(page=2)
        with pytest.raises(AttributeError):
            params.page = 3  # type: ignore[misc]
        assert repr(params) == "PageParams(page=2, size=50)"  # part of the cache key
        assert hash(params) == hash(PageParams(page=2))


class TestByLimitAndOffset:
    def test_pages(self) -> None:
        pages = FastAPIPaginationLimitOffset()
        page = pages.paginate(ITEMS, pages.params(limit=3, offset=22))
        assert isinstance(page, LimitOffsetPage)
        assert page.model_dump() == {
            "items": [{"id": 22}, {"id": 23}, {"id": 24}],
            "total": 25,
            "limit": 3,
            "offset": 22,
        }
        queried = pages.paginate(ITEMS[:2], pages.params(limit=2, offset=40), total=41)
        assert (len(queried.items), queried.total) == (2, 41)

    def test_limits(self) -> None:
        pages = FastAPIPaginationLimitOffset(default_limit=10, max_limit=20)
        assert pages.params() == LimitOffsetParams(limit=10, offset=0)
        with pytest.raises(ValueError, match="over 20"):
            pages.params(limit=21)
        with pytest.raises(ValueError, match="default limit"):
            FastAPIPaginationLimitOffset(default_limit=30, max_limit=20)
        for wrong in ({"limit": 0}, {"offset": -1}):
            with pytest.raises(ValueError, match="limit starts at 1"):
                LimitOffsetParams(**wrong)


class Event(BaseModel):
    id: int
    at: datetime


T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
EVENTS = [Event(id=i, at=T0 + timedelta(minutes=i // 2)) for i in range(1, 12)]


def by_time(event: Event) -> tuple[datetime, int]:
    return event.at, event.id


def event_ids(page: CursorPage[Event]) -> list[int]:
    return [event.id for event in page.items]


class TestByCursor:
    def test_walking_a_list(self) -> None:
        cursors, seen, cursor = FastAPIPaginationCursor(), [], None
        while True:
            page = cursors.paginate(EVENTS, cursors.params(cursor=cursor, size=4), key=by_time)
            seen.append(event_ids(page))
            if (cursor := page.next_cursor) is None:
                break
        assert seen == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11]]

    def test_newest_first(self) -> None:
        cursors, newest = FastAPIPaginationCursor(), EVENTS[::-1]
        first = cursors.paginate(newest, cursors.params(size=5), key=by_time, descending=True)
        params = cursors.params(cursor=first.next_cursor, size=5)
        second = cursors.paginate(newest, params, key=by_time, descending=True)
        assert (event_ids(first), event_ids(second)) == ([11, 10, 9, 8, 7], [6, 5, 4, 3, 2])

    def test_rows_added_or_deleted_meanwhile_dont_shift_the_pages(self) -> None:
        cursors = FastAPIPaginationCursor()
        first = cursors.paginate(EVENTS, cursors.params(size=4), key=by_time)
        changed = [e for e in EVENTS if e.id != 4] + [Event(id=0, at=T0 - timedelta(days=1))]
        changed.sort(key=by_time)
        params = cursors.params(cursor=first.next_cursor, size=4)
        assert event_ids(cursors.paginate(changed, params, key=by_time)) == [5, 6, 7, 8]

    def test_rows_queried_from_a_database(self) -> None:
        cursors = FastAPIPaginationCursor()
        params = cursors.params(size=3)
        assert (params.after, params.limit) == (None, 4)
        first = cursors.paginate(EVENTS[: params.limit], params, key=by_time, queried=True)
        assert event_ids(first) == [1, 2, 3]
        params = cursors.params(cursor=first.next_cursor, size=3)
        after = params.after
        assert after == (EVENTS[2].at, 3)  # the datetime comes back as a datetime
        assert isinstance(after, tuple)
        rows = [e for e in EVENTS if by_time(e) > after][: params.limit]  # WHERE … > … LIMIT 4
        assert event_ids(cursors.paginate(rows, params, key=by_time, queried=True)) == [4, 5, 6]
        last = cursors.paginate(EVENTS[9:], params, key=by_time, queried=True)
        assert (event_ids(last), last.next_cursor) == ([10, 11], None)

    @pytest.mark.parametrize(
        "position",
        [
            7,
            "b",
            1.5,
            True,
            Decimal("12.50"),
            UUID("8e03978e-40d5-43e8-bc93-6894a57f9324"),
            date(2026, 10, 25),
            T0,
            ("x", 1, T0),
        ],
    )
    def test_the_values_a_sort_key_can_have(self, position: Position) -> None:
        cursors = FastAPIPaginationCursor()
        decoded = cursors.decode(cursors.encode(position))
        assert (decoded, type(decoded)) == (position, type(position))

    @pytest.mark.parametrize(
        ("position", "error"),
        [
            (float("nan"), ValueError),
            (Decimal("Infinity"), ValueError),
            ((), ValueError),
            (tuple(range(9)), ValueError),
            (2**63, ValueError),
            (None, TypeError),
            ([1], TypeError),
        ],
    )
    def test_what_a_cursor_cant_hold(self, position: Any, error: type[Exception]) -> None:
        with pytest.raises(error):
            FastAPIPaginationCursor().encode(position)

    def test_end_of_the_list(self) -> None:
        cursors = FastAPIPaginationCursor()
        page = cursors.paginate(EVENTS, cursors.params(size=11), key=by_time)
        assert (len(page.items), page.next_cursor) == (11, None)
        assert cursors.paginate([], cursors.params(), key=by_time).model_dump() == {
            "items": [],
            "size": 50,
            "next_cursor": None,
        }

    def test_limits(self) -> None:
        cursors = FastAPIPaginationCursor(default_size=10, max_size=20)
        assert cursors.params() == CursorParams(size=10)
        with pytest.raises(ValueError, match="over 20"):
            cursors.params(size=21)
        with pytest.raises(ValueError, match="default size"):
            FastAPIPaginationCursor(default_size=0)
        with pytest.raises(ValueError, match="size starts at 1"):
            CursorParams(size=0)


def raw(payload: object) -> str:
    """An unsigned cursor holding `payload`, as a client could write it."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


class TestInvalidCursors:
    @pytest.mark.parametrize(
        "cursor",
        [
            "not-base64-json",
            raw({"t": "datetime", "v": "yesterday"}),
            raw({"t": "decimal", "v": "lots"}),
            raw({"t": "decimal", "v": "NaN"}),
            raw({"t": "pickle", "v": "x"}),
            raw({"t": ["datetime"], "v": "x"}),
            raw({"a": 1}),
            raw([[1, 2]]),
            raw([]),
            raw(list(range(9))),
            raw(None),
            raw(2**63),
            raw([-(2**63) - 1]),
            base64.urlsafe_b64encode(b"[NaN]").decode().rstrip("="),
            base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("="),
            "x" * 1025,
        ],
    )
    def test_are_a_400(self, cursor: str) -> None:
        with pytest.raises(HTTPException) as refused:
            FastAPIPaginationCursor().params(cursor=cursor)
        assert (refused.value.status_code, refused.value.detail) == (400, "Invalid cursor")

    def test_a_cursor_with_another_kind_of_key(self) -> None:
        cursors = FastAPIPaginationCursor()
        params = cursors.params(cursor=cursors.encode("a string"))
        with pytest.raises(HTTPException) as refused:
            cursors.paginate(EVENTS, params, key=by_time)
        assert refused.value.status_code == 400

    def test_signed_cursors(self) -> None:
        signed = FastAPIPaginationCursor(secret="s" * 32)
        cursor = signed.encode(("a", 1))
        payload, tag = cursor.split(".")
        assert signed.decode(cursor) == ("a", 1)
        assert FastAPIPaginationCursor().decode(cursor) == ("a", 1)  # readable, not secret
        for forged in (raw(["b", 1]), f"{raw(['b', 1])}.{tag}", payload, f"{payload}.{'A' * 22}"):
            with pytest.raises(HTTPException):
                signed.decode(forged)

    def test_rotating_the_secret(self) -> None:
        old = FastAPIPaginationCursor(secret="o" * 32).encode(5)
        rotated = FastAPIPaginationCursor(secret=["n" * 32, "o" * 32])
        assert rotated.decode(old) == 5
        assert rotated.encode(5) != old  # signed with the new one
        too_short = "k" * 31
        with pytest.raises(ValueError, match="32 bytes"):
            FastAPIPaginationCursor(secret=too_short)


app_pages = FastAPIPagination(default_size=10, max_size=30)
app_offsets = FastAPIPaginationLimitOffset()
app_cursors = FastAPIPaginationCursor(secret="c" * 32, default_size=4)


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="client")
def fixture_client(calls: list[str]) -> TestClient:
    FastAPICache.init(InMemoryBackend())
    app = FastAPI()

    @app.get("/items")
    @cache(expire=60)
    async def items(
        params: Annotated[PageParams, Depends(app_pages)],
        q: str | None = None,
    ) -> Page[Item]:
        calls.append("items")
        found = [item for item in ITEMS if q is None or str(item.id).startswith(q)]
        return app_pages.paginate(found, params)

    @app.get("/app_offsets")
    @cache(expire=60)
    async def by_offset(
        params: Annotated[LimitOffsetParams, Depends(app_offsets)],
    ) -> LimitOffsetPage[Item]:
        calls.append("app_offsets")
        return app_offsets.paginate(ITEMS, params)

    @app.get("/events")
    @cache(expire=60)
    async def events(params: Annotated[CursorParams, Depends(app_cursors)]) -> CursorPage[Event]:
        calls.append("events")
        return app_cursors.paginate(EVENTS, params, key=by_time)

    return TestClient(app)


class TestWithTheCache:
    def test_each_page_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        assert ids(client.get("/items?size=5").json()) == [0, 1, 2, 3, 4]
        second = client.get("/items?page=2&size=5")
        assert (second.headers["x-fastapi-cache"], ids(second.json())) == ("MISS", [5, 6, 7, 8, 9])
        again = client.get("/items?page=2&size=5")
        assert (again.headers["x-fastapi-cache"], ids(again.json())) == ("HIT", [5, 6, 7, 8, 9])
        assert ids(client.get("/items?page=2&size=6").json()) == [6, 7, 8, 9, 10, 11]
        assert len(calls) == 3

    def test_with_other_parameters(self, client: TestClient, calls: list[str]) -> None:
        body = client.get("/items?q=1&size=3").json()
        assert (ids(body), body["total"]) == ([1, 10, 11], 11)
        assert ids(client.get("/items?q=2&size=3").json()) == [2, 20, 21]
        assert len(calls) == 2

    def test_limit_and_offset(self, client: TestClient, calls: list[str]) -> None:
        assert ids(client.get("/app_offsets?limit=2&offset=10").json()) == [10, 11]
        assert ids(client.get("/app_offsets?limit=2&offset=12").json()) == [12, 13]
        assert ids(client.get("/app_offsets?limit=2&offset=10").json()) == [10, 11]
        assert calls == ["app_offsets", "app_offsets"]

    def test_each_cursor_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        first = client.get("/events").json()
        assert [e["id"] for e in first["items"]] == [1, 2, 3, 4]
        path = f"/events?cursor={first['next_cursor']}"
        assert [e["id"] for e in client.get(path).json()["items"]] == [5, 6, 7, 8]
        assert client.get(path).headers["x-fastapi-cache"] == "HIT"
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/items?page=0", 422),
            ("/items?size=31", 422),  # this tool's maximum
            ("/items?size=lots", 422),
            ("/app_offsets?limit=101", 422),
            ("/app_offsets?offset=-1", 422),
            ("/events?size=101", 422),
            ("/events?cursor=has%20spaces", 422),
            ("/events?cursor=a.b.c", 422),
            (f"/events?cursor={'x' * 2000}", 422),
            (f"/events?cursor={raw([T0.isoformat(), 3])}", 400),  # not signed
        ],
    )
    def test_wrong_parameters_never_reach_the_endpoint(
        self,
        client: TestClient,
        calls: list[str],
        path: str,
        status: int,
    ) -> None:
        response = client.get(path)
        assert response.status_code == status
        assert calls == []

    @pytest.mark.parametrize(
        ("path", "names"),
        [
            ("/items", {"page", "size", "q"}),
            ("/app_offsets", {"limit", "offset"}),
            ("/events", {"cursor", "size"}),
        ],
    )
    def test_the_parameters_are_documented(
        self,
        client: TestClient,
        path: str,
        names: set[str],
    ) -> None:
        operation = client.get("/openapi.json").json()["paths"][path]["get"]
        assert {parameter["name"] for parameter in operation["parameters"]} == names
        if path == "/items":  # with this tool's limits
            size = next(p for p in operation["parameters"] if p["name"] == "size")
            assert (size["schema"]["maximum"], size["schema"]["default"]) == (30, 10)
