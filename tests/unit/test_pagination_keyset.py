"""PaginationKeyset: pages after or before a key, from the whole list or queried from a database,
in both directions, with the cache keeping each page apart."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.cursor import PaginationCursor
from fastapi_alcazaba.pagination.keyset import KeysetPage, KeysetParams, PaginationKeyset
from fastapi_alcazaba.pagination.values import Position
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import served


class Event(BaseModel):
    id: int
    at: datetime


T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
# Two events a minute: the time alone isn't unique, (at, id) is.
EVENTS = [Event(id=i, at=T0 + timedelta(minutes=i // 2)) for i in range(1, 12)]


def by_time(event: Event) -> tuple[datetime, int]:
    return event.at, event.id


def event_ids(page: KeysetPage[Event]) -> list[int]:
    return [event.id for event in page.items]


def walk(
    keyset: PaginationKeyset,
    items: list[Event],
    *,
    limit: int,
    descending: bool = False,
) -> list[KeysetPage[Event]]:
    """Every page of the whole list, from the first to the last."""
    pages = [keyset.paginate(items, keyset.params(limit=limit), key=by_time, descending=descending)]
    while (after := pages[-1].after) is not None:
        params = keyset.params(after=after, limit=limit)
        pages.append(keyset.paginate(items, params, key=by_time, descending=descending))
    return pages


def back(
    keyset: PaginationKeyset,
    items: list[Event],
    page: KeysetPage[Event],
    *,
    descending: bool = False,
) -> list[KeysetPage[Event]]:
    """Every page before `page`, from the nearest to the first."""
    pages = []
    while (before := page.before) is not None:
        params = keyset.params(before=before, limit=page.limit)
        page = keyset.paginate(items, params, key=by_time, descending=descending)
        pages.append(page)
    return pages


class TestTheWholeList:
    def test_forwards(self) -> None:
        pages = walk(PaginationKeyset(), EVENTS, limit=4)
        assert [event_ids(page) for page in pages] == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11]]
        assert (pages[0].before, pages[-1].after) == (None, None)
        assert all(page.before is not None for page in pages[1:])

    def test_backwards_to_the_first_page(self) -> None:
        keyset = PaginationKeyset()
        last = walk(keyset, EVENTS, limit=4)[-1]
        pages = back(keyset, EVENTS, last)
        assert [event_ids(page) for page in pages] == [[5, 6, 7, 8], [1, 2, 3, 4]]
        assert all(page.after is not None for page in pages)

    def test_there_and_back_on_the_same_pages(self) -> None:
        keyset = PaginationKeyset()
        for limit in (1, 2, 3, 5, 11, 12):
            forwards = walk(keyset, EVENTS, limit=limit)
            backwards = back(keyset, EVENTS, forwards[-1])
            assert [event_ids(page) for page in backwards[::-1]] == [
                event_ids(page) for page in forwards[:-1]
            ]

    def test_newest_first(self) -> None:
        keyset, newest = PaginationKeyset(), EVENTS[::-1]
        pages = walk(keyset, newest, limit=5, descending=True)
        assert [event_ids(page) for page in pages] == [
            [11, 10, 9, 8, 7],
            [6, 5, 4, 3, 2],
            [1],
        ]
        backwards = back(keyset, newest, pages[-1], descending=True)
        assert [event_ids(page) for page in backwards] == [[6, 5, 4, 3, 2], [11, 10, 9, 8, 7]]

    def test_back_from_a_page_that_isnt_on_the_grid(self) -> None:
        """The page before ends right before the key, and stops at the start of the list: it's
        the first page, shorter than the limit."""
        keyset = PaginationKeyset()
        params = keyset.params(after=keyset.encode(by_time(EVENTS[1])), limit=4)
        page = keyset.paginate(EVENTS, params, key=by_time)
        assert event_ids(page) == [3, 4, 5, 6]
        assert page.before is not None
        params = keyset.params(before=page.before, limit=4)
        first = keyset.paginate(EVENTS, params, key=by_time)
        assert (event_ids(first), first.before) == ([1, 2], None)
        assert first.after is not None
        assert keyset.decode(first.after) == by_time(EVENTS[1])

    def test_rows_added_or_deleted_meanwhile_dont_shift_the_pages(self) -> None:
        keyset = PaginationKeyset()
        second = walk(keyset, EVENTS, limit=4)[1]
        changed = [e for e in EVENTS if e.id not in (3, 9)] + [
            Event(id=0, at=T0 - timedelta(days=1)),
            Event(id=12, at=T0 + timedelta(days=1)),
        ]
        changed.sort(key=by_time)
        assert second.after is not None
        assert second.before is not None
        after = keyset.paginate(changed, keyset.params(after=second.after, limit=4), key=by_time)
        assert event_ids(after) == [10, 11, 12]
        before = keyset.paginate(changed, keyset.params(before=second.before, limit=4), key=by_time)
        assert event_ids(before) == [0, 1, 2, 4]

    def test_keys_past_either_end(self) -> None:
        keyset = PaginationKeyset()
        late = keyset.encode((T0 + timedelta(days=1), 0))
        early = keyset.encode((T0 - timedelta(days=1), 0))
        past = keyset.paginate(EVENTS, keyset.params(after=late, limit=4), key=by_time)
        assert past.model_dump() == {"items": [], "limit": 4, "before": None, "after": None}
        start = keyset.paginate(EVENTS, keyset.params(before=early, limit=4), key=by_time)
        assert start.model_dump() == {"items": [], "limit": 4, "before": None, "after": None}
        everything = keyset.paginate(EVENTS, keyset.params(after=early, limit=4), key=by_time)
        assert (event_ids(everything), everything.before) == ([1, 2, 3, 4], None)
        to_the_end = keyset.paginate(EVENTS, keyset.params(before=late, limit=4), key=by_time)
        assert (event_ids(to_the_end), to_the_end.after) == ([8, 9, 10, 11], None)

    def test_an_empty_list(self) -> None:
        keyset = PaginationKeyset()
        assert keyset.paginate([], keyset.params(), key=by_time).model_dump() == {
            "items": [],
            "limit": 50,
            "before": None,
            "after": None,
        }


def query(params: KeysetParams, items: list[Event] = EVENTS) -> list[Event]:
    """What a database returns: WHERE (at, id) > :after ORDER BY at, id LIMIT :fetch, or
    WHERE (at, id) < :before ORDER BY at DESC, id DESC LIMIT :fetch."""
    if isinstance(params.before, tuple):
        before = params.before
        return [e for e in items[::-1] if by_time(e) < before][: params.fetch]
    after = params.after
    if isinstance(after, tuple):
        return [e for e in items if by_time(e) > after][: params.fetch]
    return items[: params.fetch]


class TestQueriedRows:
    def test_forwards_and_back(self) -> None:
        keyset = PaginationKeyset()
        params = keyset.params(limit=4)
        assert (params.after, params.before, params.fetch) == (None, None, 5)
        first = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(first), first.before) == ([1, 2, 3, 4], None)
        assert first.after is not None
        params = keyset.params(after=first.after, limit=4)
        assert params.after == (EVENTS[3].at, 4)  # the datetime comes back as a datetime
        second = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert event_ids(second) == [5, 6, 7, 8]
        assert second.after is not None
        assert second.before is not None
        params = keyset.params(after=second.after, limit=4)
        third = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(third), third.after) == ([9, 10, 11], None)
        assert third.before is not None
        params = keyset.params(before=third.before, limit=4)
        assert params.before == (EVENTS[8].at, 9)
        rows = query(params)
        assert [e.id for e in rows] == [8, 7, 6, 5, 4]  # nearest first, and one more
        again = keyset.paginate(rows, params, key=by_time, queried=True)
        assert again == second
        params = keyset.params(before=again.before, limit=4)
        assert keyset.paginate(query(params), params, key=by_time, queried=True) == first

    def test_the_first_page_backwards(self) -> None:
        """Exactly a page before the key: no extra row, so no page before it."""
        keyset = PaginationKeyset()
        params = keyset.params(before=keyset.encode(by_time(EVENTS[4])), limit=4)
        page = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(page), page.before) == ([1, 2, 3, 4], None)
        assert page.after is not None
        params = keyset.params(before=keyset.encode(by_time(EVENTS[2])), limit=4)
        short = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(short), short.before) == ([1, 2], None)

    def test_the_last_page_exactly(self) -> None:
        keyset = PaginationKeyset()
        params = keyset.params(after=keyset.encode(by_time(EVENTS[6])), limit=4)
        page = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert (event_ids(page), page.after) == ([8, 9, 10, 11], None)

    def test_nothing_left(self) -> None:
        keyset = PaginationKeyset()
        params = keyset.params(after=keyset.encode(by_time(EVENTS[-1])), limit=4)
        empty = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert (empty.items, empty.before, empty.after) == ([], None, None)
        params = keyset.params(before=keyset.encode(by_time(EVENTS[0])), limit=4)
        empty = keyset.paginate(query(params), params, key=by_time, queried=True)
        assert (empty.items, empty.before, empty.after) == ([], None, None)

    def test_newest_first(self) -> None:
        """Descending: after is WHERE key < :after ORDER BY key DESC, before is WHERE key >
        :before ORDER BY key, nearest first too."""
        keyset, newest = PaginationKeyset(), EVENTS[::-1]
        params = keyset.params(limit=3)
        first = keyset.paginate(newest[: params.fetch], params, key=by_time, queried=True)
        assert first.after is not None
        params = keyset.params(after=first.after, limit=3)
        after = params.after
        assert isinstance(after, tuple)
        rows = [e for e in newest if by_time(e) < after][: params.fetch]
        second = keyset.paginate(rows, params, key=by_time, descending=True, queried=True)
        assert event_ids(second) == [8, 7, 6]
        assert second.before is not None
        params = keyset.params(before=second.before, limit=3)
        before = params.before
        assert isinstance(before, tuple)
        rows = [e for e in EVENTS if by_time(e) > before][: params.fetch]
        assert [e.id for e in rows] == [9, 10, 11]
        again = keyset.paginate(rows, params, key=by_time, descending=True, queried=True)
        assert (event_ids(again), again.before) == ([11, 10, 9], None)


def raw(payload: object) -> str:
    """An unsigned key holding `payload`, as a client could write it."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


def at(
    keyset: PaginationKeyset,
    name: str,
    key: str,
) -> KeysetParams:
    """The parameters of the page after `key`, or before it."""
    return keyset.params(after=key) if name == "after" else keyset.params(before=key)


class TestParameters:
    def test_limits(self) -> None:
        keyset = PaginationKeyset(default_limit=10, max_limit=20)
        assert keyset.params() == KeysetParams(limit=10)
        with pytest.raises(ValueError, match="over 20"):
            keyset.params(limit=21)
        with pytest.raises(ValueError, match="default limit"):
            PaginationKeyset(default_limit=0)
        with pytest.raises(ValueError, match="limit starts at 1"):
            KeysetParams(limit=0)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            KeysetParams(after=1, before=2)
        params = KeysetParams(limit=5, after=(T0, 3))
        with pytest.raises(AttributeError):
            params.limit = 6  # type: ignore[misc]
        assert repr(params) == (  # part of the cache key
            "KeysetParams(limit=5, after=(datetime.datetime(2026, 10, 25, 12, 0, "
            "tzinfo=datetime.timezone.utc), 3), before=None)"
        )
        assert hash(params) == hash(KeysetParams(limit=5, after=(T0, 3)))

    def test_after_and_before_together_are_a_400(self) -> None:
        keyset = PaginationKeyset()
        with pytest.raises(HTTPException) as refused:
            keyset.params(after=keyset.encode(1), before=keyset.encode(2))
        assert (refused.value.status_code, refused.value.detail) == (
            400,
            "Give after or before, not both",
        )

    @pytest.mark.parametrize("name", ["after", "before"])
    @pytest.mark.parametrize(
        "key",
        [
            "not-base64-json",
            raw({"t": "datetime", "v": "yesterday"}),
            raw({"a": 1}),
            raw([]),
            raw(None),
            "x" * 1025,
        ],
    )
    def test_invalid_keys_are_a_400(self, name: str, key: str) -> None:
        with pytest.raises(HTTPException) as refused:
            at(PaginationKeyset(), name, key)
        assert (refused.value.status_code, refused.value.detail) == (400, f"Invalid {name}")

    @pytest.mark.parametrize("name", ["after", "before"])
    def test_a_key_with_another_kind_of_sort_key(self, name: str) -> None:
        keyset = PaginationKeyset()
        params = at(keyset, name, keyset.encode("a string"))
        with pytest.raises(HTTPException) as refused:
            keyset.paginate(EVENTS, params, key=by_time)
        assert (refused.value.status_code, refused.value.detail) == (400, f"Invalid {name}")

    @pytest.mark.parametrize("position", [7, "b", 1.5, True, T0, ("x", 1, T0)])
    def test_the_values_a_key_can_hold(self, position: Position) -> None:
        keyset = PaginationKeyset()
        decoded = keyset.decode(keyset.encode(position))
        assert (decoded, type(decoded)) == (position, type(position))

    def test_signed_keys(self) -> None:
        signed = PaginationKeyset(secret="k" * 32)
        key = signed.encode(("a", 1))
        payload, _ = key.split(".")
        assert signed.decode(key) == ("a", 1)
        assert PaginationKeyset().decode(key) == ("a", 1)  # readable, not secret
        for forged in (raw(["b", 1]), payload, f"{payload}.{'A' * 22}"):
            with pytest.raises(HTTPException):
                signed.decode(forged)

    def test_a_cursor_signed_with_the_same_secret_isnt_a_key(self) -> None:
        cursor = PaginationCursor(secret="k" * 32).encode(("a", 1))
        with pytest.raises(HTTPException):
            PaginationKeyset(secret="k" * 32).decode(cursor)


app_keyset = PaginationKeyset(secret="s" * 32, default_limit=4)


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="client")
def fixture_client(calls: list[str]) -> TestClient:
    FastAPICache.init(InMemoryBackend())
    app = FastAPI()

    @app.get("/events")
    @cache(expire=60)
    async def events(params: Annotated[KeysetParams, Depends(app_keyset)]) -> KeysetPage[Event]:
        calls.append("events")
        return app_keyset.paginate(query(params), params, key=by_time, queried=True)

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
        assert links(second)["prev"] == f"/events?kind=login&before={body['before']}&limit=4"
        assert links(second)["next"] == f"/events?kind=login&after={body['after']}&limit=4"
        third = client.get(links(second)["next"])
        assert (ids(third.json()), set(links(third))) == ([9, 10, 11], {"first", "prev"})
        returned = client.get(links(third)["prev"])
        assert ids(returned.json()) == [5, 6, 7, 8]
        assert "after=" not in links(returned)["prev"]
        assert "before=" not in links(returned)["next"]
        assert ids(client.get(links(returned)["prev"]).json()) == [1, 2, 3, 4]

    def test_each_key_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        key = client.get("/events").json()["after"]
        assert served(client.get(f"/events?after={key}").headers) == "miss"
        hit = client.get(f"/events?after={key}")
        assert (served(hit.headers), ids(hit.json())) == ("hit", [5, 6, 7, 8])
        assert set(links(hit)) == {"first", "prev", "next"}
        before = client.get(f"/events?before={key}")
        assert (served(before.headers), ids(before.json())) == ("miss", [1, 2, 3])
        assert len(calls) == 3

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/events?limit=101", 422),
            ("/events?after=has%20spaces", 422),
            ("/events?before=a.b.c", 422),
            (f"/events?after={'x' * 2000}", 422),
            (f"/events?after={raw([T0.isoformat(), 3])}", 400),  # not signed
            (f"/events?before={raw([T0.isoformat(), 3])}", 400),
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

    def test_after_and_before_together(self, client: TestClient, calls: list[str]) -> None:
        key = client.get("/events").json()["after"]
        response = client.get(f"/events?after={key}&before={key}")
        assert (response.status_code, response.json()) == (
            400,
            {"detail": "Give after or before, not both"},
        )
        assert calls == ["events"]

    def test_the_parameters_are_documented(self, client: TestClient) -> None:
        operation = client.get("/openapi.json").json()["paths"]["/events"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"after", "before", "limit"}
        assert parameters["limit"]["schema"]["default"] == 4
