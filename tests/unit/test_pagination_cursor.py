"""PaginationCursor: pages after or before a cursor, from the whole list or queried from a
database, in both directions; cursors signed, bound to the query and, with a ttl, expiring; the
cache keeping each page apart."""

import base64
import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.cursor import CursorPage, CursorParams, PaginationCursor
from fastapi_alcazaba.pagination.values import Position, Sealer
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import served

SECRET = "s" * 32


class Event(BaseModel):
    id: int
    at: datetime


T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
# Two events a minute: the time alone isn't unique, (at, id) is.
EVENTS = [Event(id=i, at=T0 + timedelta(minutes=i // 2)) for i in range(1, 12)]


def by_time(event: Event) -> tuple[datetime, int]:
    return event.at, event.id


def event_ids(page: CursorPage[Event]) -> list[int]:
    return [event.id for event in page.items]


def walk(
    cursors: PaginationCursor,
    items: list[Event],
    *,
    limit: int,
    descending: bool = False,
) -> list[CursorPage[Event]]:
    """Every page of the whole list, from the first to the last."""
    first = cursors.paginate(items, cursors.params(limit=limit), key=by_time, descending=descending)
    pages = [first]
    while (cursor := pages[-1].next_cursor) is not None:
        params = cursors.params(cursor=cursor, limit=limit)
        pages.append(cursors.paginate(items, params, key=by_time, descending=descending))
    return pages


def back(
    cursors: PaginationCursor,
    items: list[Event],
    page: CursorPage[Event],
    *,
    descending: bool = False,
) -> list[CursorPage[Event]]:
    """Every page before `page`, from the nearest to the first."""
    pages = []
    while (cursor := page.prev_cursor) is not None:
        params = cursors.params(cursor=cursor, limit=page.limit)
        page = cursors.paginate(items, params, key=by_time, descending=descending)
        pages.append(page)
    return pages


def after(cursors: PaginationCursor, position: Position, limit: int = 4) -> CursorParams:
    return cursors.params(cursor=cursors.encode(position), limit=limit)


def before(cursors: PaginationCursor, position: Position, limit: int = 4) -> CursorParams:
    return cursors.params(cursor=cursors.encode(position, before=True), limit=limit)


class TestTheWholeList:
    def test_forwards(self) -> None:
        pages = walk(PaginationCursor(SECRET), EVENTS, limit=4)
        assert [event_ids(page) for page in pages] == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11]]
        assert (pages[0].prev_cursor, pages[-1].next_cursor) == (None, None)
        assert all(page.prev_cursor is not None for page in pages[1:])

    def test_backwards_to_the_first_page(self) -> None:
        cursors = PaginationCursor(SECRET)
        last = walk(cursors, EVENTS, limit=4)[-1]
        pages = back(cursors, EVENTS, last)
        assert [event_ids(page) for page in pages] == [[5, 6, 7, 8], [1, 2, 3, 4]]
        assert all(page.next_cursor is not None for page in pages)

    def test_there_and_back_on_the_same_pages(self) -> None:
        cursors = PaginationCursor(SECRET)
        for limit in (1, 2, 3, 5, 11, 12):
            forwards = walk(cursors, EVENTS, limit=limit)
            backwards = back(cursors, EVENTS, forwards[-1])
            assert [event_ids(page) for page in backwards[::-1]] == [
                event_ids(page) for page in forwards[:-1]
            ]

    def test_newest_first(self) -> None:
        cursors, newest = PaginationCursor(SECRET), EVENTS[::-1]
        pages = walk(cursors, newest, limit=5, descending=True)
        assert [event_ids(page) for page in pages] == [[11, 10, 9, 8, 7], [6, 5, 4, 3, 2], [1]]
        backwards = back(cursors, newest, pages[-1], descending=True)
        assert [event_ids(page) for page in backwards] == [[6, 5, 4, 3, 2], [11, 10, 9, 8, 7]]

    def test_back_from_a_page_that_isnt_on_the_grid(self) -> None:
        """The page before ends right before the position, and stops at the start of the list:
        it's the first page, shorter than the limit."""
        cursors = PaginationCursor(SECRET)
        page = cursors.paginate(EVENTS, after(cursors, by_time(EVENTS[1])), key=by_time)
        assert event_ids(page) == [3, 4, 5, 6]
        params = cursors.params(cursor=page.prev_cursor, limit=4)
        first = cursors.paginate(EVENTS, params, key=by_time)
        assert (event_ids(first), first.prev_cursor) == ([1, 2], None)
        assert cursors.params(cursor=first.next_cursor).after == by_time(EVENTS[1])

    def test_rows_added_or_deleted_meanwhile_dont_shift_the_pages(self) -> None:
        cursors = PaginationCursor(SECRET)
        second = walk(cursors, EVENTS, limit=4)[1]
        changed = [e for e in EVENTS if e.id not in (3, 9)] + [
            Event(id=0, at=T0 - timedelta(days=1)),
            Event(id=12, at=T0 + timedelta(days=1)),
        ]
        changed.sort(key=by_time)
        params = cursors.params(cursor=second.next_cursor, limit=4)
        assert event_ids(cursors.paginate(changed, params, key=by_time)) == [10, 11, 12]
        params = cursors.params(cursor=second.prev_cursor, limit=4)
        assert event_ids(cursors.paginate(changed, params, key=by_time)) == [0, 1, 2, 4]

    def test_positions_past_either_end(self) -> None:
        cursors = PaginationCursor(SECRET)
        late, early = (T0 + timedelta(days=1), 0), (T0 - timedelta(days=1), 0)
        empty = {"items": [], "limit": 4, "prev_cursor": None, "next_cursor": None}
        assert cursors.paginate(EVENTS, after(cursors, late), key=by_time).model_dump() == empty
        assert cursors.paginate(EVENTS, before(cursors, early), key=by_time).model_dump() == empty
        everything = cursors.paginate(EVENTS, after(cursors, early), key=by_time)
        assert (event_ids(everything), everything.prev_cursor) == ([1, 2, 3, 4], None)
        to_the_end = cursors.paginate(EVENTS, before(cursors, late), key=by_time)
        assert (event_ids(to_the_end), to_the_end.next_cursor) == ([8, 9, 10, 11], None)

    def test_an_empty_list(self) -> None:
        cursors = PaginationCursor(SECRET)
        assert cursors.paginate([], cursors.params(), key=by_time).model_dump() == {
            "items": [],
            "limit": 50,
            "prev_cursor": None,
            "next_cursor": None,
        }


def query(params: CursorParams, items: list[Event] = EVENTS) -> list[Event]:
    """What a database returns: WHERE (at, id) > :after ORDER BY at, id LIMIT :fetch, or
    WHERE (at, id) < :before ORDER BY at DESC, id DESC LIMIT :fetch."""
    if isinstance(params.before, tuple):
        position = params.before
        return [e for e in items[::-1] if by_time(e) < position][: params.fetch]
    if isinstance(params.after, tuple):
        position = params.after
        return [e for e in items if by_time(e) > position][: params.fetch]
    return items[: params.fetch]


class TestQueriedRows:
    def test_forwards_and_back(self) -> None:
        cursors = PaginationCursor(SECRET)
        params = cursors.params(limit=4)
        assert (params.after, params.before, params.fetch) == (None, None, 5)
        first = cursors.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(first), first.prev_cursor) == ([1, 2, 3, 4], None)
        params = cursors.params(cursor=first.next_cursor, limit=4)
        assert params.after == (EVENTS[3].at, 4)  # the datetime comes back as a datetime
        second = cursors.paginate(query(params), params, key=by_time, queried=True)
        assert event_ids(second) == [5, 6, 7, 8]
        params = cursors.params(cursor=second.next_cursor, limit=4)
        third = cursors.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(third), third.next_cursor) == ([9, 10, 11], None)
        params = cursors.params(cursor=third.prev_cursor, limit=4)
        assert params.before == (EVENTS[8].at, 9)
        rows = query(params)
        assert [e.id for e in rows] == [8, 7, 6, 5, 4]  # nearest first, and one more
        again = cursors.paginate(rows, params, key=by_time, queried=True)
        assert again == second
        params = cursors.params(cursor=again.prev_cursor, limit=4)
        assert cursors.paginate(query(params), params, key=by_time, queried=True) == first

    def test_the_first_page_backwards(self) -> None:
        """Exactly a page before the position: no extra row, so no page before it."""
        cursors = PaginationCursor(SECRET)
        params = before(cursors, by_time(EVENTS[4]))
        page = cursors.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(page), page.prev_cursor) == ([1, 2, 3, 4], None)
        assert page.next_cursor is not None
        params = before(cursors, by_time(EVENTS[2]))
        short = cursors.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(short), short.prev_cursor) == ([1, 2], None)

    def test_the_last_page_exactly(self) -> None:
        cursors = PaginationCursor(SECRET)
        params = after(cursors, by_time(EVENTS[6]))
        page = cursors.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(page), page.next_cursor) == ([8, 9, 10, 11], None)

    def test_nothing_left(self) -> None:
        cursors = PaginationCursor(SECRET)
        for params in (after(cursors, by_time(EVENTS[-1])), before(cursors, by_time(EVENTS[0]))):
            empty = cursors.paginate(query(params), params, key=by_time, queried=True)
            assert (empty.items, empty.prev_cursor, empty.next_cursor) == ([], None, None)

    def test_newest_first(self) -> None:
        """Descending: after is WHERE key < :after ORDER BY key DESC, before is WHERE key >
        :before ORDER BY key, nearest first too."""
        cursors, newest = PaginationCursor(SECRET), EVENTS[::-1]
        params = cursors.params(limit=3)
        first = cursors.paginate(newest[: params.fetch], params, key=by_time, queried=True)
        params = cursors.params(cursor=first.next_cursor, limit=3)
        assert isinstance(position := params.after, tuple)
        rows = [e for e in newest if by_time(e) < position][: params.fetch]
        second = cursors.paginate(rows, params, key=by_time, descending=True, queried=True)
        assert event_ids(second) == [8, 7, 6]
        params = cursors.params(cursor=second.prev_cursor, limit=3)
        assert isinstance(position := params.before, tuple)
        rows = [e for e in EVENTS if by_time(e) > position][: params.fetch]
        assert [e.id for e in rows] == [9, 10, 11]
        again = cursors.paginate(rows, params, key=by_time, descending=True, queried=True)
        assert (event_ids(again), again.prev_cursor) == ([11, 10, 9], None)


def refused(cursors: PaginationCursor, **params: Any) -> str:
    """The detail of the 400 that `params` gets."""
    with pytest.raises(HTTPException) as caught:
        cursors.params(**params)
    assert caught.value.status_code == 400
    return str(caught.value.detail)


class TestParameters:
    def test_limits(self) -> None:
        cursors = PaginationCursor(SECRET, default_limit=10, max_limit=20)
        assert cursors.params() == CursorParams(limit=10)
        with pytest.raises(ValueError, match="over 20"):
            cursors.params(limit=21)
        with pytest.raises(ValueError, match="default limit"):
            PaginationCursor(SECRET, default_limit=0)
        with pytest.raises(ValueError, match="limit starts at 1"):
            CursorParams(limit=0)
        with pytest.raises(ValueError, match="ttl"):
            PaginationCursor(SECRET, ttl=0)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            CursorParams(after=1, before=2)
        params = PaginationCursor(SECRET).params(limit=5, query=[("genre", "poetry")])
        with pytest.raises(AttributeError):
            params.limit = 6  # type: ignore[misc]
        # Part of the cache key: the query's digest keeps the pages of each query apart.
        assert repr(params) == (
            "CursorParams(limit=5, cursor=None, after=None, before=None, "
            f"query_digest='{params.query_digest}')"
        )
        assert params != PaginationCursor(SECRET).params(limit=5, query=[("genre", "novel")])
        same = PaginationCursor(SECRET).params(limit=5, query=[("genre", "poetry")])
        assert hash(params) == hash(same)

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
        cursors = PaginationCursor(SECRET)
        decoded = cursors.params(cursor=cursors.encode(position)).after
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
            PaginationCursor(SECRET).encode(position)

    def test_a_cursor_with_another_kind_of_sort_key(self) -> None:
        cursors = PaginationCursor(SECRET)
        for params in (after(cursors, "a string"), before(cursors, "a string")):
            with pytest.raises(HTTPException) as caught:
                cursors.paginate(EVENTS, params, key=by_time)
            assert (caught.value.status_code, caught.value.detail) == (400, "Invalid cursor")


class TestBoundToTheQuery:
    def test_a_cursor_of_other_filters_is_a_400(self) -> None:
        cursors = PaginationCursor(SECRET)
        params = cursors.params(limit=5, query=[("kind", "login")])
        cursor = cursors.paginate(EVENTS, params, key=by_time).next_cursor
        assert cursors.params(cursor=cursor, query=[("kind", "login")]).after == by_time(EVENTS[4])
        for other in ([("kind", "logout")], [], [("kind", "login"), ("user", "ana")]):
            detail = refused(cursors, cursor=cursor, query=other)
            assert detail == "cursor was issued for other query parameters"

    def test_order_and_the_page_parameters_dont_count(self) -> None:
        cursors = PaginationCursor(SECRET)
        pairs = [("kind", "login"), ("tag", "a"), ("tag", "b")]
        params = cursors.params(limit=5, query=pairs)
        cursor = cursors.paginate(EVENTS, params, key=by_time).next_cursor
        assert cursor is not None
        same = [("tag", "b"), ("limit", "7"), ("kind", "login"), ("cursor", cursor), ("tag", "a")]
        assert cursors.params(cursor=cursor, query=same).after == by_time(EVENTS[4])

    def test_the_next_cursors_keep_the_binding(self) -> None:
        cursors = PaginationCursor(SECRET)
        pairs = [("kind", "login")]
        first = cursors.paginate(EVENTS, cursors.params(limit=5, query=pairs), key=by_time)
        params = cursors.params(cursor=first.next_cursor, limit=5, query=pairs)
        second = cursors.paginate(EVENTS, params, key=by_time)
        for cursor in (second.next_cursor, second.prev_cursor):
            assert cursors.params(cursor=cursor, query=pairs).cursor == cursor
            assert refused(cursors, cursor=cursor).startswith("cursor was issued")

    def test_encode_for_a_query(self) -> None:
        cursors = PaginationCursor(SECRET)
        cursor = cursors.encode(by_time(EVENTS[0]), query=[("kind", "login")])
        assert cursors.params(cursor=cursor, query=[("kind", "login")]).after == by_time(EVENTS[0])
        assert refused(cursors, cursor=cursor).startswith("cursor was issued")


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1_800_000_000.0
        monkeypatch.setattr(time, "time", lambda: self.now)


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


class TestExpiry:
    def test_a_cursor_lasts_ttl_seconds(self, clock: Clock) -> None:
        cursors = PaginationCursor(SECRET, ttl=600)
        cursor = cursors.paginate(EVENTS, cursors.params(limit=5), key=by_time).next_cursor
        clock.now += 600
        assert cursors.params(cursor=cursor).after == by_time(EVENTS[4])
        clock.now += 1
        assert refused(cursors, cursor=cursor) == "Expired cursor"

    def test_from_when_its_page_was_made(self, clock: Clock) -> None:
        cursors = PaginationCursor(SECRET, ttl=600)
        first = cursors.paginate(EVENTS, cursors.params(limit=5), key=by_time)
        clock.now += 500
        second = cursors.paginate(
            EVENTS, cursors.params(cursor=first.next_cursor, limit=5), key=by_time
        )
        clock.now += 500  # the first cursor has expired, the second one hasn't
        assert refused(cursors, cursor=first.next_cursor) == "Expired cursor"
        assert cursors.params(cursor=second.next_cursor).after == by_time(EVENTS[9])

    def test_without_a_ttl_a_page_always_has_the_same_cursors(self, clock: Clock) -> None:
        cursors = PaginationCursor(SECRET)
        first = cursors.paginate(EVENTS, cursors.params(limit=5), key=by_time)
        clock.now += 10 * 365 * 24 * 3600
        assert cursors.paginate(EVENTS, cursors.params(limit=5), key=by_time) == first
        assert cursors.params(cursor=first.next_cursor).after == by_time(EVENTS[4])


def raw(payload: object) -> str:
    """An unsigned cursor holding `payload`, as a client could write it."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


class TestTampering:
    def test_cursors_that_were_made_up_or_changed(self) -> None:
        cursors = PaginationCursor(SECRET)
        cursor = cursors.paginate(EVENTS, cursors.params(limit=5), key=by_time).next_cursor
        assert cursor is not None
        payload, tag = cursor.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        way, position, digest, issued = json.loads(base64.urlsafe_b64decode(padded))
        assert (way, position[1]) == ("a", 5)  # readable, not secret
        for forged in (
            raw([way, [position[0], 9], digest, issued]),
            f"{raw([way, [position[0], 9], digest, issued])}.{tag}",
            payload,
            f"{payload}.{'A' * 22}",
            f"{payload}.{tag[:-1]}é",
            "é",
            "x" * 1025,
            "not-a-cursor",
        ):
            assert refused(cursors, cursor=forged) == "Invalid cursor"

    def test_signed_by_another_secret_or_for_another_purpose(self) -> None:
        other = PaginationCursor("o" * 32).encode(5)
        assert refused(PaginationCursor(SECRET), cursor=other) == "Invalid cursor"
        scroll = Sealer(SECRET, b"fastapi_alcazaba.scroll").seal(["a", 5, "x", 0])
        assert refused(PaginationCursor(SECRET), cursor=scroll) == "Invalid cursor"

    @pytest.mark.parametrize(
        "payload",
        [
            ["c", 5, "d", 0],
            ["a", 5, "d"],
            ["a", 5, "d", 0, 0],
            ["a", 5, 1, 0],
            ["a", 5, "d", True],
            ["a", 5, "d", 1.5],
            ["a", {"t": "pickle", "v": "x"}, "d", 0],
            ["a", [], "d", 0],
            ["a", list(range(9)), "d", 0],
            ["a", 2**63, "d", 0],
            ["a", None, "d", 0],
            {"after": 5},
            5,
            None,
        ],
    )
    def test_signed_but_not_a_cursor(self, payload: object) -> None:
        cursor = Sealer(SECRET, b"fastapi_alcazaba.cursor").seal(payload)
        assert refused(PaginationCursor(SECRET), cursor=cursor) == "Invalid cursor"


class TestRotatingTheSecret:
    def test_old_cursors_are_accepted_new_ones_signed_with_the_new_secret(self) -> None:
        old = PaginationCursor("o" * 32).encode(5)
        rotated = PaginationCursor(["n" * 32, "o" * 32])
        assert rotated.params(cursor=old).after == 5
        new = rotated.encode(5)
        assert new != old
        assert PaginationCursor("n" * 32).params(cursor=new).after == 5
        assert refused(PaginationCursor("o" * 32), cursor=new) == "Invalid cursor"

    def test_a_secret_is_needed_and_long_enough(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            PaginationCursor("k" * 31)
        with pytest.raises(ValueError, match="at least one key"):
            PaginationCursor([])


app_cursors = PaginationCursor(SECRET, default_limit=4)


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="client")
def fixture_client(calls: list[str]) -> TestClient:
    FastAPICache.init(InMemoryBackend())
    app = FastAPI()

    @app.get("/events")
    @cache(expire=60)
    async def events(params: Annotated[CursorParams, Depends(app_cursors)]) -> CursorPage[Event]:
        calls.append("events")
        return app_cursors.paginate(query(params), params, key=by_time, queried=True)

    return TestClient(app)


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def links(response: httpx.Response) -> dict[str, str]:
    """The Link header as a client reads it: relation -> target."""
    return {rel: link["url"] for rel, link in response.links.items() if rel is not None}


class TestInAnApp:
    def test_both_ways_through_the_links(self, client: TestClient) -> None:
        first = client.get("/events?kind=login")
        assert ids(first.json()) == [1, 2, 3, 4]
        assert set(links(first)) == {"first", "next"}
        second = client.get(links(first)["next"])
        assert ids(second.json()) == [5, 6, 7, 8]
        assert links(second)["first"] == "/events?kind=login&limit=4"
        body = second.json()
        assert links(second)["prev"] == f"/events?kind=login&cursor={body['prev_cursor']}&limit=4"
        assert links(second)["next"] == f"/events?kind=login&cursor={body['next_cursor']}&limit=4"
        third = client.get(links(second)["next"])
        assert (ids(third.json()), set(links(third))) == ([9, 10, 11], {"first", "prev"})
        returned = client.get(links(third)["prev"])
        assert ids(returned.json()) == [5, 6, 7, 8]
        assert ids(client.get(links(returned)["prev"]).json()) == [1, 2, 3, 4]

    def test_each_cursor_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        cursor = client.get("/events").json()["next_cursor"]
        assert served(client.get(f"/events?cursor={cursor}").headers) == "miss"
        hit = client.get(f"/events?cursor={cursor}")
        assert (served(hit.headers), ids(hit.json())) == ("hit", [5, 6, 7, 8])
        assert set(links(hit)) == {"first", "prev", "next"}
        assert len(calls) == 2

    def test_each_query_has_its_own_pages_and_cursors(
        self,
        client: TestClient,
        calls: list[str],
    ) -> None:
        """A parameter that the endpoint doesn't read still binds the cursor: a page cached for
        one query never hands its cursor to another, where it would be refused."""
        mail = client.get("/events?ref=mail")
        feed = client.get("/events?ref=feed")
        assert (served(feed.headers), len(calls)) == ("miss", 2)
        for ref, page in (("mail", mail), ("feed", feed)):
            cursor = page.json()["next_cursor"]
            assert ids(client.get(f"/events?ref={ref}&cursor={cursor}").json()) == [5, 6, 7, 8]
        assert served(client.get("/events?ref=mail").headers) == "hit"

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/events?limit=101", 422),
            ("/events?cursor=has%20spaces", 422),
            ("/events?cursor=a.b.c", 422),
            (f"/events?cursor={'x' * 2000}", 422),
            (f"/events?cursor={raw(['a', [T0.isoformat(), 3], 'x', 0])}", 400),  # not signed
            (f"/events?kind=logout&cursor={app_cursors.encode(3, query=[('kind', 'login')])}", 400),
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
        operation = client.get("/openapi.json").json()["paths"]["/events"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"cursor", "limit"}
        assert parameters["limit"]["schema"]["default"] == 4
