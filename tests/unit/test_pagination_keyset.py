"""PaginationKeyset: pages after a sort key that clients read and write, a value as text or
several as a JSON array, converted to the declared types, from the whole list or queried from a
database."""

import json
from datetime import UTC, date, datetime, timedelta, timezone
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
from fastapi_alcazaba.pagination.keyset import KeysetPage, KeysetParams, PaginationKeyset
from fastapi_alcazaba.pagination.values import CursorValue
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
    events: PaginationKeyset,
    items: list[Event],
    *,
    limit: int,
    descending: bool = False,
) -> list[KeysetPage[Event]]:
    """Every page of the whole list, sending back the after of each."""
    pages = [events.paginate(items, events.params(limit=limit), key=by_time, descending=descending)]
    while pages[-1].has_more:
        params = events.params(after=pages[-1].after, limit=limit)
        pages.append(events.paginate(items, params, key=by_time, descending=descending))
    return pages


class TestTheWholeList:
    def test_walking_it(self) -> None:
        pages = walk(PaginationKeyset(sort=(datetime, int)), EVENTS, limit=4)
        assert [event_ids(page) for page in pages] == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11]]
        assert pages[0].after == '["2026-10-25T12:02:00Z",4]'
        assert (pages[-1].after, pages[-1].has_more) == ('["2026-10-25T12:05:00Z",11]', False)

    def test_newest_first(self) -> None:
        pages = walk(PaginationKeyset((datetime, int)), EVENTS[::-1], limit=5, descending=True)
        assert [event_ids(page) for page in pages] == [[11, 10, 9, 8, 7], [6, 5, 4, 3, 2], [1]]

    def test_values_a_client_wrote(self) -> None:
        """Not only the last item's: the events after noon past one minute, whatever their id."""
        events = PaginationKeyset((datetime, int))
        params = events.params(after='["2026-10-25T12:01:00Z", 0]', limit=3)
        assert params.after == (T0 + timedelta(minutes=1), 0)
        assert event_ids(events.paginate(EVENTS, params, key=by_time)) == [2, 3, 4]
        other_zone = '["2026-10-25T14:01:00+02:00", 0]'  # the same instant
        params = events.params(after=other_zone, limit=3)
        assert event_ids(events.paginate(EVENTS, params, key=by_time)) == [2, 3, 4]

    def test_rows_added_or_deleted_meanwhile_dont_shift_the_pages(self) -> None:
        events = PaginationKeyset((datetime, int))
        first = events.paginate(EVENTS, events.params(limit=4), key=by_time)
        changed = [e for e in EVENTS if e.id != 4] + [Event(id=0, at=T0 - timedelta(days=1))]
        changed.sort(key=by_time)
        params = events.params(after=first.after, limit=4)
        assert event_ids(events.paginate(changed, params, key=by_time)) == [5, 6, 7, 8]

    def test_the_end_of_the_list(self) -> None:
        events = PaginationKeyset((datetime, int))
        page = events.paginate(EVENTS, events.params(limit=11), key=by_time)
        assert (len(page.items), page.has_more) == (11, False)
        past = events.params(after='["2027-01-01T00:00:00Z", 0]')
        assert events.paginate(EVENTS, past, key=by_time).model_dump() == {
            "items": [],
            "limit": 50,
            "after": None,
            "has_more": False,
        }


class TestQueriedRows:
    def test_walking_them(self) -> None:
        events = PaginationKeyset((datetime, int))
        params = events.params(limit=3)
        assert (params.after, params.fetch) == (None, 4)
        first = events.paginate(EVENTS[: params.fetch], params, key=by_time, queried=True)
        assert (event_ids(first), first.after) == ([1, 2, 3], '["2026-10-25T12:01:00Z",3]')
        params = events.params(after=first.after, limit=3)
        after = params.after
        assert after == (EVENTS[2].at, 3)
        assert isinstance(after, tuple)
        rows = [e for e in EVENTS if by_time(e) > after][: params.fetch]  # WHERE … > … LIMIT 4
        assert event_ids(events.paginate(rows, params, key=by_time, queried=True)) == [4, 5, 6]
        last = events.paginate(EVENTS[8:], params, key=by_time, queried=True)
        assert (event_ids(last), last.has_more) == ([9, 10, 11], False)

    def test_one_value(self) -> None:
        numbers = PaginationKeyset((int,))
        params = numbers.params(after="[7]", limit=2)
        assert params.after == (7,)
        page = numbers.paginate([8, 9, 10], params, key=lambda n: (n,), queried=True)
        assert (page.items, page.after) == ([8, 9], "[9]")


class TestTheValues:
    @pytest.mark.parametrize(
        ("kind", "text", "value"),
        [
            (int, "42", 42),
            (int, "-9223372036854775808", -(2**63)),
            (float, "1.5", 1.5),
            (float, "2", 2.0),
            (float, "1e3", 1000.0),
            (str, '"olive"', "olive"),
            (bool, "true", True),
            (datetime, '"2026-10-29T10:00:00Z"', datetime(2026, 10, 29, 10, tzinfo=UTC)),
            (
                datetime,
                '"2026-10-29T10:00:00.250+02:00"',
                datetime(2026, 10, 29, 8, 0, 0, 250_000, UTC),
            ),
            (date, '"2026-10-29"', date(2026, 10, 29)),
            (
                UUID,
                '"8e03978e-40d5-43e8-bc93-6894a57f9324"',
                UUID("8e03978e-40d5-43e8-bc93-6894a57f9324"),
            ),
            (Decimal, '"12.50"', Decimal("12.50")),
            (Decimal, "0.1000000000000000000001", Decimal("0.1000000000000000000001")),  # exact
            (Decimal, "3", Decimal(3)),
        ],
    )
    def test_converted_to_their_types(self, kind: type[CursorValue], text: str, value: Any) -> None:
        decoded = PaginationKeyset((kind,)).decode(f"[{text}]")
        assert isinstance(decoded, tuple)
        assert decoded == (value,)
        assert type(decoded[0]) is kind

    @pytest.mark.parametrize(
        ("position", "dumped"),
        [
            ((7, 1.5, "a", False), [7, 1.5, "a", False]),
            ((T0, date(2026, 10, 25)), ["2026-10-25T12:00:00Z", "2026-10-25"]),
            ((UUID(int=1), Decimal("12.50")), ["00000000-0000-0000-0000-000000000001", "12.50"]),
        ],
    )
    def test_there_and_back(self, position: tuple[CursorValue, ...], dumped: list[Any]) -> None:
        events = PaginationKeyset(tuple(type(value) for value in position))
        page = events.paginate([position, position], events.params(limit=1), key=lambda p: p)
        assert page.after is not None
        assert json.loads(page.after) == dumped
        assert events.params(after=page.after).after == position

    @pytest.mark.parametrize(
        ("sort", "after"),
        [
            ((int,), "not json"),
            ((int,), "42"),  # not an array
            ((int,), "{}"),
            ((int,), "[]"),
            ((int,), "[1, 2]"),  # one too many
            ((datetime, int), '["2026-10-29T10:00:00Z"]'),  # one too few
            ((int,), '["42"]'),
            ((int,), "[true]"),
            ((int,), "[1.5]"),
            ((int,), f"[{2**63}]"),
            ((int,), "[null]"),
            ((float,), "[NaN]"),
            ((float,), "[Infinity]"),
            ((float,), "[1e400]"),
            ((float,), f"[{10**400}]"),
            ((float,), '["1.5"]'),
            ((float,), "[false]"),
            ((str,), "[1]"),
            ((bool,), "[1]"),
            ((datetime,), '["2026-10-29T10:00:00"]'),  # no time zone
            ((datetime,), '["yesterday"]'),
            ((datetime,), "[1793286953]"),
            ((date,), '["2026-10-29T10:00:00Z"]'),
            ((UUID,), '["not-a-uuid"]'),
            ((Decimal,), '["lots"]'),
            ((Decimal,), '["NaN"]'),
            ((Decimal,), "[true]"),
            ((int,), "[" * 600 + "]" * 600),
            ((str,), f'["{"x" * 1030}"]'),
        ],
    )
    def test_anything_else_is_a_400(
        self,
        sort: tuple[type[CursorValue], ...],
        after: str,
    ) -> None:
        with pytest.raises(HTTPException) as refused:
            PaginationKeyset(sort).params(after=after)
        assert (refused.value.status_code, refused.value.detail) == (400, "Invalid after")

    @pytest.mark.parametrize(
        ("sort", "position"),
        [
            ((datetime, int), (T0, "1")),
            ((datetime, int), (T0.replace(tzinfo=None), 1)),  # no time zone
            ((datetime, int), (T0, True)),
            ((datetime, int), (T0, 2**63)),
            ((date,), (T0,)),  # a datetime isn't a date here
            ((Decimal,), (1.5,)),
            ((float,), (float("nan"),)),
            ((datetime, int), (T0,)),
            ((int,), 1),  # not a tuple
        ],
    )
    def test_a_key_that_doesnt_match_sort(
        self,
        sort: tuple[type[CursorValue], ...],
        position: Any,
    ) -> None:
        events = PaginationKeyset(sort)
        with pytest.raises(TypeError):
            events.paginate([1, 2], events.params(limit=1), key=lambda _: position)

    def test_the_sort_types(self) -> None:
        for wrong in ((), (int,) * 9):
            with pytest.raises(ValueError, match="1 to 8"):
                PaginationKeyset(wrong)
        with pytest.raises(TypeError, match="bytes"):
            PaginationKeyset((bytes,))  # type: ignore[arg-type]  # what the check is for
        assert PaginationKeyset([datetime, int]).sort == (datetime, int)


class TestParameters:
    def test_limits(self) -> None:
        events = PaginationKeyset((int,), default_limit=10, max_limit=20)
        assert events.params() == KeysetParams(limit=10)
        with pytest.raises(ValueError, match="over 20"):
            events.params(limit=21)
        with pytest.raises(ValueError, match="default limit"):
            PaginationKeyset((int,), default_limit=0)
        with pytest.raises(ValueError, match="limit starts at 1"):
            KeysetParams(limit=0)
        with pytest.raises(ValueError, match="at least one"):
            KeysetParams(after=())

    def test_params_are_checked_immutable_and_stable(self) -> None:
        params = PaginationKeyset((datetime, int)).params(
            after='["2026-10-25T12:00:00Z", 3]',
            limit=5,
        )
        with pytest.raises(AttributeError):
            params.limit = 6  # type: ignore[misc]
        assert repr(params) == (  # part of the cache key
            "KeysetParams(limit=5, after=(datetime.datetime(2026, 10, 25, 12, 0, "
            "tzinfo=datetime.timezone.utc), 3))"
        )
        assert params == KeysetParams(limit=5, after=(T0, 3))


app_events = PaginationKeyset((datetime, int), default_limit=4)


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="client")
def fixture_client(calls: list[str]) -> TestClient:
    FastAPICache.init(InMemoryBackend())
    app = FastAPI()

    @app.get("/events")
    @cache(expire=60)
    async def events(
        params: Annotated[KeysetParams, Depends(app_events)],
    ) -> KeysetPage[Event]:
        calls.append("events")
        return app_events.paginate(EVENTS, params, key=by_time)

    return TestClient(app)


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def links(response: httpx.Response) -> dict[str, str]:
    """The Link header as a client reads it: relation -> target."""
    return {rel: link["url"] for rel, link in response.links.items() if rel is not None}


class TestInAnApp:
    def test_through_the_links(self, client: TestClient) -> None:
        first = client.get("/events?kind=login")
        assert first.json()["after"] == '["2026-10-25T12:02:00Z",4]'
        assert links(first) == {
            "first": "/events?kind=login&limit=4",
            "next": "/events?kind=login&after=%5B%222026-10-25T12%3A02%3A00Z%22%2C4%5D&limit=4",
        }
        second = client.get(links(first)["next"])
        assert ids(second.json()) == [5, 6, 7, 8]
        third = client.get(links(second)["next"])
        assert (ids(third.json()), set(links(third))) == ([9, 10, 11], {"first"})

    def test_each_position_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        url = "/events?after=[%222026-10-25T12:02:00Z%22,4]"
        assert served(client.get(url).headers) == "miss"
        hit = client.get(url)
        assert (served(hit.headers), ids(hit.json())) == ("hit", [5, 6, 7, 8])
        assert set(links(hit)) == {"first", "next"}
        other = client.get("/events?after=[%222026-10-25T12:02:00Z%22,5]")
        assert (served(other.headers), ids(other.json())) == ("miss", [6, 7, 8, 9])
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/events?limit=101", 422),
            (f"/events?after=[{'1,' * 600}1]", 422),
            ("/events?after=[4]", 400),
            ("/events?after=[%222026-10-25T12:02:00%22,4]", 400),  # no time zone
            ("/events?after=[%222026-10-25T12:02:00Z%22,%224%22]", 400),
            ("/events?after=nope", 400),
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
        if status == 400:
            assert response.json() == {"detail": "Invalid after"}
        assert calls == []

    def test_the_parameters_are_documented(self, client: TestClient) -> None:
        operation = client.get("/openapi.json").json()["paths"]["/events"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"after", "limit"}
        assert parameters["after"]["schema"]["anyOf"][0]["maxLength"] == 1024


def test_a_time_zone_of_its_own_is_kept() -> None:
    events = PaginationKeyset((datetime, int))
    madrid = timezone(timedelta(hours=1))
    at = datetime(2026, 10, 25, 13, 0, tzinfo=madrid)
    page = events.paginate([(at, 1), (at, 2)], events.params(limit=1), key=lambda p: p)
    assert page.after == '["2026-10-25T13:00:00+01:00",1]'


class Message(BaseModel):
    id: int


MESSAGES = [Message(id=i) for i in range(1, 8)]


class TestOneKey:
    """A key of one value, written as text: an id, a time, a name."""

    def test_by_id(self) -> None:
        messages = PaginationKeyset(sort=int)
        first = messages.paginate(MESSAGES, messages.params(limit=3), key=lambda m: m.id)
        assert ([m.id for m in first.items], first.after, first.has_more) == ([1, 2, 3], "3", True)
        params = messages.params(after=first.after, limit=3)
        assert params.after == 3
        second = messages.paginate(MESSAGES, params, key=lambda m: m.id)
        assert [m.id for m in second.items] == [4, 5, 6]

    def test_syncing_from_the_last_page(self) -> None:
        """The last page still says where it ended: asked again later, it gives what came since."""
        messages = PaginationKeyset(sort=int)
        last = messages.paginate(MESSAGES, messages.params(after="6"), key=lambda m: m.id)
        assert ([m.id for m in last.items], last.after, last.has_more) == ([7], "7", False)
        later = [*MESSAGES, Message(id=8), Message(id=9)]
        since = messages.paginate(later, messages.params(after=last.after), key=lambda m: m.id)
        assert [m.id for m in since.items] == [8, 9]
        empty = messages.paginate(MESSAGES, messages.params(after="7"), key=lambda m: m.id)
        assert (empty.items, empty.after, empty.has_more) == ([], None, False)

    def test_newest_first(self) -> None:
        messages = PaginationKeyset(sort=int)
        newest = MESSAGES[::-1]
        params = messages.params(after="5", limit=3)
        page = messages.paginate(newest, params, key=lambda m: m.id, descending=True)
        assert [m.id for m in page.items] == [4, 3, 2]

    @pytest.mark.parametrize(
        ("kind", "text", "value"),
        [
            (int, "42", 42),
            (float, "1.5", 1.5),
            (str, "olive oil", "olive oil"),
            (str, "42", "42"),
            (bool, "false", False),
            (datetime, "2026-10-29T10:00:00Z", datetime(2026, 10, 29, 10, tzinfo=UTC)),
            (date, "2026-10-29", date(2026, 10, 29)),
            (UUID, "00000000-0000-0000-0000-000000000001", UUID(int=1)),
            (Decimal, "12.50", Decimal("12.50")),
        ],
    )
    def test_there_and_back(self, kind: type[CursorValue], text: str, value: Any) -> None:
        keyset = PaginationKeyset(sort=kind)
        decoded = keyset.params(after=text).after
        assert (decoded, type(decoded)) == (value, kind)
        page = keyset.paginate([value, value], keyset.params(limit=1), key=lambda v: v)
        assert page.after == text

    @pytest.mark.parametrize(
        ("kind", "text"),
        [
            (int, "abc"),
            (int, "1.5"),
            (int, "[1]"),
            (int, str(2**63)),
            (float, "NaN"),
            (bool, "yes"),
            (datetime, "2026-10-29T10:00:00"),  # no time zone
            (date, "yesterday"),
            (UUID, "not-a-uuid"),
            (Decimal, "lots"),
            (str, "x" * 1025),
        ],
    )
    def test_anything_else_is_a_400(self, kind: type[CursorValue], text: str) -> None:
        with pytest.raises(HTTPException) as refused:
            PaginationKeyset(sort=kind).params(after=text)
        assert (refused.value.status_code, refused.value.detail) == (400, "Invalid after")

    def test_a_key_of_another_type(self) -> None:
        messages = PaginationKeyset(sort=int)
        with pytest.raises(TypeError):
            messages.paginate(MESSAGES, messages.params(limit=1), key=lambda m: str(m.id))

    def test_in_an_app(self) -> None:
        app, messages = FastAPI(), PaginationKeyset(sort=int, default_limit=3)

        @app.get("/messages")
        async def list_messages(
            params: Annotated[KeysetParams, Depends(messages)],
        ) -> KeysetPage[Message]:
            return messages.paginate(MESSAGES, params, key=lambda m: m.id)

        client = TestClient(app)
        first = client.get("/messages")
        assert links(first)["next"] == "/messages?after=3&limit=3"
        assert ids(client.get(links(first)["next"]).json()) == [4, 5, 6]
        assert client.get("/messages?after=x").json() == {"detail": "Invalid after"}
