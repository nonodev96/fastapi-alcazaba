"""PaginationTimeBased: pages older or newer than a timestamp, that never split the items sharing
a time, from the whole list or queried from a database."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.time_based import (
    PaginationTimeBased,
    TimeBasedPage,
    TimeBasedParams,
)
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import served


class Post(BaseModel):
    id: int
    at: datetime


T0 = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
MINUTES = [0, 1, 1, 1, 2, 3, 3, 4, 5, 5, 5, 5, 6]  # posts that share a time: 2-4, 6-7, 9-12
POSTS = [Post(id=i, at=T0 + timedelta(minutes=m)) for i, m in enumerate(MINUTES, start=1)]
NEWEST = POSTS[::-1]  # 13, 12... with the ties in some order of their own


def at(post: Post) -> datetime:
    return post.at


def minute(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def post_ids(page: TimeBasedPage[Post]) -> list[int]:
    return [post.id for post in page.items]


def query(
    feed: PaginationTimeBased,
    params: TimeBasedParams,
) -> list[Post]:
    """What a database returns: WHERE at < :timestamp ORDER BY at DESC LIMIT :fetch ("older"),
    or WHERE at > :timestamp ORDER BY at LIMIT :fetch ("newer")."""
    timestamp = params.timestamp
    if feed.direction == "older":
        return [p for p in NEWEST if timestamp is None or p.at < timestamp][: params.fetch]
    return [p for p in POSTS if timestamp is None or p.at > timestamp][: params.fetch]


def walk(
    feed: PaginationTimeBased,
    *,
    limit: int,
    queried: bool,
) -> list[TimeBasedPage[Post]]:
    """Every page, sending back the next_timestamp of each."""
    items = NEWEST if feed.direction == "older" else POSTS
    pages: list[TimeBasedPage[Post]] = []
    timestamp = None
    while not pages or (timestamp := pages[-1].next_timestamp) is not None:
        params = feed.params(timestamp=timestamp, limit=limit)
        rows = query(feed, params) if queried else items
        pages.append(feed.paginate(rows, params, time=at, queried=queried))
    return pages


class TestTheWholeList:
    def test_a_page_takes_the_rest_of_the_items_of_its_last_time(self) -> None:
        feed = PaginationTimeBased()
        pages = walk(feed, limit=3, queried=False)
        assert [post_ids(page) for page in pages] == [
            [13, 12, 11, 10, 9],  # 9-12 share 10:05
            [8, 7, 6],
            [5, 4, 3, 2],  # 2-4 share 10:01
            [1],
        ]
        assert [page.next_timestamp for page in pages] == [minute(5), minute(3), minute(1), None]
        assert all(page.limit == 3 for page in pages)

    def test_a_group_that_ends_with_the_page(self) -> None:
        feed = PaginationTimeBased("newer")
        page = feed.paginate(POSTS, feed.params(limit=4), time=at)
        assert (post_ids(page), page.next_timestamp) == ([1, 2, 3, 4], minute(1))

    @pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 13, 14])
    @pytest.mark.parametrize("direction", ["older", "newer"])
    def test_every_item_once(self, direction: Any, limit: int) -> None:
        pages = walk(PaginationTimeBased(direction), limit=limit, queried=False)
        expected = NEWEST if direction == "older" else POSTS
        assert [post.id for page in pages for post in page.items] == [p.id for p in expected]
        assert all(page.items for page in pages)

    def test_newer(self) -> None:
        feed = PaginationTimeBased("newer")
        pages = walk(feed, limit=3, queried=False)
        assert [post_ids(page) for page in pages] == [
            [1, 2, 3, 4],
            [5, 6, 7],
            [8, 9, 10, 11, 12],
            [13],
        ]

    def test_a_timestamp_of_the_client(self) -> None:
        """The posts before 10:05, from Madrid's clock: strictly before, whatever the zone."""
        feed = PaginationTimeBased()
        madrid = datetime(2026, 10, 29, 11, 5, tzinfo=timezone(timedelta(hours=1)))
        params = feed.params(timestamp=madrid, limit=2)
        assert params.timestamp == minute(5)
        assert params.timestamp is not None
        assert params.timestamp.tzinfo is UTC
        assert post_ids(feed.paginate(NEWEST, params, time=at)) == [8, 7, 6]
        newer = PaginationTimeBased("newer")
        params = newer.params(timestamp=madrid, limit=2)
        assert post_ids(newer.paginate(POSTS, params, time=at)) == [13]

    def test_past_either_end(self) -> None:
        feed = PaginationTimeBased()
        early = feed.params(timestamp=minute(0))
        assert feed.paginate(NEWEST, early, time=at).model_dump() == {
            "items": [],
            "limit": 50,
            "next_timestamp": None,
        }
        late = feed.paginate(NEWEST, feed.params(timestamp=minute(60), limit=13), time=at)
        assert (len(late.items), late.next_timestamp) == (13, None)
        assert feed.paginate([], feed.params(), time=at).items == []


class TestQueriedRows:
    def test_the_last_time_is_left_to_the_next_page(self) -> None:
        feed = PaginationTimeBased()
        params = feed.params(limit=3)
        assert (params.timestamp, params.fetch) == (None, 4)
        rows = query(feed, params)
        assert [p.id for p in rows] == [13, 12, 11, 10]  # 12, 11 and 10 share 10:05
        first = feed.paginate(rows, params, time=at, queried=True)
        assert (post_ids(first), first.next_timestamp) == ([13], minute(6))
        params = feed.params(timestamp=first.next_timestamp, limit=3)
        rows = query(feed, params)
        assert [p.id for p in rows] == [12, 11, 10, 9]
        with pytest.raises(ValueError, match="more than 3 items share the time") as raised:
            feed.paginate(rows, params, time=at, queried=True)
        assert "PaginationKeyset" in str(raised.value)

    def test_a_group_as_big_as_the_page(self) -> None:
        feed = PaginationTimeBased()
        params = feed.params(timestamp=minute(6), limit=4)
        page = feed.paginate(query(feed, params), params, time=at, queried=True)
        assert (post_ids(page), page.next_timestamp) == ([12, 11, 10, 9], minute(5))
        params = feed.params(timestamp=page.next_timestamp, limit=4)
        page = feed.paginate(query(feed, params), params, time=at, queried=True)
        assert post_ids(page) == [8, 7, 6, 5]

    @pytest.mark.parametrize("limit", [4, 5, 6, 13, 14])
    @pytest.mark.parametrize("direction", ["older", "newer"])
    def test_every_item_once(self, direction: Any, limit: int) -> None:
        """With pages that hold the biggest group (4), no item is skipped nor seen twice."""
        pages = walk(PaginationTimeBased(direction), limit=limit, queried=True)
        expected = NEWEST if direction == "older" else POSTS
        assert [post.id for page in pages for post in page.items] == [p.id for p in expected]
        assert all(len(page.items) <= limit for page in pages)

    def test_newer(self) -> None:
        feed = PaginationTimeBased("newer")
        pages = walk(feed, limit=5, queried=True)
        assert [post_ids(page) for page in pages] == [
            [1, 2, 3, 4, 5],
            [6, 7, 8],
            [9, 10, 11, 12, 13],
        ]
        assert [page.next_timestamp for page in pages] == [minute(2), minute(4), None]

    def test_exactly_the_last_page(self) -> None:
        feed = PaginationTimeBased()
        params = feed.params(timestamp=minute(2), limit=4)
        rows = query(feed, params)
        assert len(rows) == 4
        page = feed.paginate(rows, params, time=at, queried=True)
        assert (post_ids(page), page.next_timestamp) == ([4, 3, 2, 1], None)


class TestParameters:
    def test_limits_and_direction(self) -> None:
        feed = PaginationTimeBased(default_limit=10, max_limit=20)
        assert (feed.direction, feed.params()) == ("older", TimeBasedParams(limit=10))
        with pytest.raises(ValueError, match="over 20"):
            feed.params(limit=21)
        with pytest.raises(ValueError, match="default limit"):
            PaginationTimeBased(default_limit=0)
        with pytest.raises(ValueError, match="limit starts at 1"):
            TimeBasedParams(limit=0)
        with pytest.raises(ValueError, match="older"):
            PaginationTimeBased("sideways")  # type: ignore[arg-type]  # what the check is for

    def test_a_timestamp_needs_a_time_zone(self) -> None:
        with pytest.raises(ValueError, match="time zone"):
            PaginationTimeBased().params(timestamp=datetime(2026, 10, 29, 10))
        feed = PaginationTimeBased()
        naive = [
            Post(id=1, at=datetime(2026, 10, 29, 10)),
            Post(id=2, at=datetime(2026, 10, 29, 9)),
        ]
        with pytest.raises(ValueError, match="time zone"):
            feed.paginate(naive, feed.params(limit=1), time=at)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        params = PaginationTimeBased().params(timestamp=minute(5), limit=5)
        with pytest.raises(AttributeError):
            params.limit = 6  # type: ignore[misc]
        assert repr(params) == (  # part of the cache key
            "TimeBasedParams(limit=5, timestamp=datetime.datetime(2026, 10, 29, 10, 5, "
            "tzinfo=datetime.timezone.utc))"
        )
        assert hash(params) == hash(TimeBasedParams(limit=5, timestamp=minute(5)))


app_feed = PaginationTimeBased(default_limit=3)


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="client")
def fixture_client(calls: list[str]) -> TestClient:
    FastAPICache.init(InMemoryBackend())
    app = FastAPI()

    @app.get("/posts")
    @cache(expire=60)
    async def posts(params: Annotated[TimeBasedParams, Depends(app_feed)]) -> TimeBasedPage[Post]:
        calls.append("posts")
        return app_feed.paginate(NEWEST, params, time=at)

    return TestClient(app)


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def links(response: httpx.Response) -> dict[str, str]:
    """The Link header as a client reads it: relation -> target."""
    return {rel: link["url"] for rel, link in response.links.items() if rel is not None}


class TestInAnApp:
    def test_through_the_links(self, client: TestClient) -> None:
        first = client.get("/posts?tag=olive")
        assert (ids(first.json()), first.json()["next_timestamp"]) == (
            [13, 12, 11, 10, 9],
            "2026-10-29T10:05:00Z",
        )
        assert links(first) == {
            "first": "/posts?tag=olive&limit=3",
            "next": "/posts?tag=olive&timestamp=2026-10-29T10%3A05%3A00Z&limit=3",
        }
        second = client.get(links(first)["next"])
        assert ids(second.json()) == [8, 7, 6]
        assert ids(client.get(links(second)["next"]).json()) == [5, 4, 3, 2]

    def test_one_entry_per_instant(self, client: TestClient, calls: list[str]) -> None:
        utc = client.get("/posts?timestamp=2026-10-29T10:05:00Z")
        assert (served(utc.headers), ids(utc.json())) == ("miss", [8, 7, 6])
        madrid = client.get("/posts?timestamp=2026-10-29T11:05:00%2B01:00")
        assert (served(madrid.headers), ids(madrid.json())) == ("hit", [8, 7, 6])
        assert links(madrid)["first"] == "/posts?limit=3"
        assert len(calls) == 1

    @pytest.mark.parametrize(
        "path",
        [
            "/posts?timestamp=2026-10-29T10:05:00",  # no time zone
            "/posts?timestamp=yesterday",
            "/posts?limit=101",
            "/posts?limit=0",
        ],
    )
    def test_wrong_parameters_are_a_422(
        self,
        client: TestClient,
        calls: list[str],
        path: str,
    ) -> None:
        assert client.get(path).status_code == 422
        assert calls == []

    def test_the_parameters_are_documented(self, client: TestClient) -> None:
        operation = client.get("/openapi.json").json()["paths"]["/posts"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"timestamp", "limit"}
        assert parameters["limit"]["schema"]["default"] == 3
