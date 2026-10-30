"""PaginationRange: ranges of items by the indexes of the first and last, with the total in the
page and in Content-Range."""

from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.ranges import PaginationRange, RangePage, RangeParams
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import served


class Item(BaseModel):
    id: int


ITEMS = [Item(id=i) for i in range(25)]


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def item_ids(page: RangePage[Item]) -> list[int]:
    return [item.id for item in page.items]


class TestByRange:
    def test_ranges_of_a_list(self) -> None:
        ranges = PaginationRange()
        page = ranges.paginate(ITEMS, ranges.params(start=10, end=12))
        assert isinstance(page, RangePage)
        assert page.model_dump() == {
            "items": [{"id": 10}, {"id": 11}, {"id": 12}],
            "start": 10,
            "end": 12,
            "total": 25,
            "limit": 3,
        }
        assert page.headers() == {"Content-Range": "items 10-12/25"}

    def test_the_last_range(self) -> None:
        ranges = PaginationRange()
        last = ranges.paginate(ITEMS, ranges.params(start=20, end=29))
        assert (item_ids(last), last.end, last.limit) == ([20, 21, 22, 23, 24], 24, 10)
        assert last.headers() == {"Content-Range": "items 20-24/25"}

    def test_an_empty_list(self) -> None:
        ranges = PaginationRange()
        for start in (0, 40):
            page = ranges.paginate(list[Item](), ranges.params(start=start))
            assert (page.items, page.end, page.total) == ([], None, 0)
            assert page.headers() == {"Content-Range": "items */0"}

    def test_past_the_last_item_is_a_416(self) -> None:
        ranges = PaginationRange()
        for start in (25, 90):
            with pytest.raises(HTTPException) as refused:
                ranges.paginate(ITEMS, ranges.params(start=start))
            assert refused.value.status_code == 416
            assert refused.value.headers == {"Content-Range": "items */25"}
        with pytest.raises(HTTPException):
            ranges.paginate([], ranges.params(start=319), total=319)

    def test_a_range_queried_from_a_database(self) -> None:
        ranges = PaginationRange()
        params = ranges.params(start=0, end=24)
        assert (params.offset, params.limit) == (0, 25)
        rows = ITEMS[params.offset : params.offset + params.limit]  # LIMIT 25 OFFSET 0
        page = ranges.paginate(rows, params, total=319)
        assert (len(page.items), page.end, page.total) == (25, 24, 319)
        assert page.headers() == {"Content-Range": "items 0-24/319"}
        with pytest.raises(ValueError, match="negative"):
            ranges.paginate([], params, total=-1)

    def test_end_defaults_to_the_default_size(self) -> None:
        ranges = PaginationRange(default_items=25, max_items=200)
        assert ranges.params() == RangeParams(start=0, end=24)
        assert ranges.params(start=100) == RangeParams(start=100, end=124)
        assert ranges.params(start=7, end=7).limit == 1
        assert ranges.params(start=0, end=199).limit == 200

    @pytest.mark.parametrize(
        ("start", "end", "detail"),
        [
            (10, 9, "Invalid range: end is before start"),
            (0, 100, "Invalid range: more than 100 items"),
            (50, 150, "Invalid range: more than 100 items"),
        ],
    )
    def test_wrong_ranges_are_a_400(self, start: int, end: int, detail: str) -> None:
        with pytest.raises(HTTPException) as refused:
            PaginationRange().params(start=start, end=end)
        assert (refused.value.status_code, refused.value.detail) == (400, detail)

    def test_limits(self) -> None:
        for wrong in ({"default_items": 0}, {"default_items": 101}):
            with pytest.raises(ValueError, match="default items"):
                PaginationRange(**wrong)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        for start, end in ((-1, 10), (10, 9)):
            with pytest.raises(ValueError, match="end can't be before it"):
                RangeParams(start=start, end=end)
        params = RangeParams(start=25, end=49)
        with pytest.raises(AttributeError):
            params.start = 50  # type: ignore[misc]
        assert repr(params) == "RangeParams(start=25, end=49)"  # part of the cache key
        assert hash(params) == hash(RangeParams(start=25, end=49))

    def test_none_outside_a_request(self) -> None:
        ranges = PaginationRange()
        params = ranges.params(start=5)
        assert params.links is None
        assert ranges.paginate(ITEMS, params).start == 5


app_ranges = PaginationRange(default_items=5, max_items=10)


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
        params: Annotated[RangeParams, Depends(app_ranges)],
        q: str | None = None,
    ) -> RangePage[Item]:
        calls.append("items")
        found = [item for item in ITEMS if q is None or str(item.id).startswith(q)]
        return app_ranges.paginate(found, params)

    return TestClient(app)


def links(response: httpx.Response) -> dict[str, str]:
    """The Link header as a client reads it: relation -> target."""
    return {rel: link["url"] for rel, link in response.links.items() if rel is not None}


class TestInAnApp:
    def test_each_range_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        assert ids(client.get("/items").json()) == [0, 1, 2, 3, 4]
        second = client.get("/items?start=5&end=9")
        assert (served(second.headers), ids(second.json())) == ("miss", [5, 6, 7, 8, 9])
        assert second.headers["Content-Range"] == "items 5-9/25"
        again = client.get("/items?start=5&end=9")
        assert (served(again.headers), ids(again.json())) == ("hit", [5, 6, 7, 8, 9])
        assert again.headers["Content-Range"] == "items 5-9/25"  # the cache adds it to hits
        assert ids(client.get("/items?start=5&end=6").json()) == [5, 6]
        assert len(calls) == 3

    def test_an_empty_list(self, client: TestClient) -> None:
        response = client.get("/items?q=99&start=10")
        assert (response.status_code, response.json()["end"]) == (200, None)
        assert response.headers["Content-Range"] == "items */0"

    def test_past_the_last_item(self, client: TestClient, calls: list[str]) -> None:
        for _ in range(2):  # an error isn't cached
            response = client.get("/items?start=25")
            assert (response.status_code, response.json()) == (
                416,
                {"detail": "Range Not Satisfiable"},
            )
            assert response.headers["Content-Range"] == "items */25"
            assert "link" not in response.headers
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/items?start=-1", 422),
            ("/items?end=-1", 422),
            ("/items?start=first", 422),
            ("/items?start=5&end=4", 400),
            ("/items?start=0&end=10", 400),  # this tool's maximum
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
        operation = client.get("/openapi.json").json()["paths"]["/items"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"start", "end", "q"}
        assert parameters["start"]["schema"]["minimum"] == 0
        assert "start + 4" in parameters["end"]["description"]  # with this tool's limits

    def test_links(self, client: TestClient) -> None:
        response = client.get("/items?start=7&end=11")
        assert response.headers["Link"] == (
            '</items?start=0&end=4>; rel="first", </items?start=2&end=6>; rel="prev", '
            '</items?start=12&end=16>; rel="next", </items?start=20&end=24>; rel="last"'
        )
        assert links(client.get("/items")) == {
            "first": "/items?start=0&end=4",
            "next": "/items?start=5&end=9",
            "last": "/items?start=20&end=24",
        }

    def test_links_at_the_end_keep_the_size_asked(self, client: TestClient) -> None:
        response = client.get("/items?start=20&end=29")
        assert response.headers["Content-Range"] == "items 20-24/25"
        assert links(response) == {
            "first": "/items?start=0&end=9",
            "prev": "/items?start=10&end=19",
            "last": "/items?start=20&end=29",
        }
        assert links(client.get("/items?q=99")) == {
            "first": "/items?q=99&start=0&end=4",
            "last": "/items?q=99&start=0&end=4",
        }

    def test_hits_get_the_links_of_their_own_request(
        self,
        client: TestClient,
        calls: list[str],
    ) -> None:
        first = client.get("/items?start=5&end=9&ref=mail")
        again = client.get("/items?start=5&end=9&ref=feed")
        assert (served(again.headers), len(calls)) == ("hit", 1)
        assert links(first)["next"] == "/items?ref=mail&start=10&end=14"
        assert links(again)["next"] == "/items?ref=feed&start=10&end=14"
