"""PaginationOffset: pages by offset and limit, with the total or without it, when one more row
tells a page apart from the last."""

from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.offset import OffsetPage, OffsetParams, PaginationOffset
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import served


class Item(BaseModel):
    id: int


ITEMS = [Item(id=i) for i in range(25)]


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def item_ids(page: OffsetPage[Item]) -> list[int]:
    return [item.id for item in page.items]


class TestByOffset:
    def test_pages_of_a_list(self) -> None:
        offsets = PaginationOffset()
        page = offsets.paginate(ITEMS, offsets.params(offset=10, limit=5))
        assert isinstance(page, OffsetPage)
        assert page.model_dump() == {
            "items": [{"id": 10}, {"id": 11}, {"id": 12}, {"id": 13}, {"id": 14}],
            "offset": 10,
            "limit": 5,
            "has_more": True,
            "total": 25,  # a whole list is counted
        }

    def test_the_last_page(self) -> None:
        offsets = PaginationOffset()
        last = offsets.paginate(ITEMS, offsets.params(offset=20, limit=5))
        assert (item_ids(last), last.has_more) == ([20, 21, 22, 23, 24], False)
        short = offsets.paginate(ITEMS, offsets.params(offset=22, limit=5))
        assert (item_ids(short), short.has_more) == ([22, 23, 24], False)
        past = offsets.paginate(ITEMS, offsets.params(offset=90, limit=5))
        assert (past.items, past.has_more) == ([], False)
        assert offsets.paginate([], offsets.params()).model_dump() == {
            "items": [],
            "offset": 0,
            "limit": 50,
            "has_more": False,
            "total": 0,
        }

    def test_rows_queried_from_a_database(self) -> None:
        offsets = PaginationOffset()
        params = offsets.params(offset=10, limit=5)
        assert params.fetch == 6
        rows = ITEMS[params.offset : params.offset + params.fetch]  # LIMIT 6 OFFSET 10
        page = offsets.paginate(rows, params, queried=True)
        assert (item_ids(page), page.has_more) == ([10, 11, 12, 13, 14], True)
        params = offsets.params(offset=20, limit=5)
        last = offsets.paginate(ITEMS[20:26], params, queried=True)
        assert (item_ids(last), last.has_more) == ([20, 21, 22, 23, 24], False)

    def test_limits(self) -> None:
        offsets = PaginationOffset(default_limit=10, max_limit=20)
        assert offsets.params() == OffsetParams(offset=0, limit=10)
        assert offsets.params(limit=20).fetch == 21
        with pytest.raises(ValueError, match="over 20"):
            offsets.params(limit=21)
        for wrong in ({"default_limit": 0}, {"default_limit": 30, "max_limit": 20}):
            with pytest.raises(ValueError, match="default limit"):
                PaginationOffset(**wrong)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        for offset, limit in ((-1, 10), (0, 0)):
            with pytest.raises(ValueError, match="offset starts at 0 and limit at 1"):
                OffsetParams(offset=offset, limit=limit)
        params = OffsetParams(offset=40, limit=20)
        with pytest.raises(AttributeError):
            params.offset = 60  # type: ignore[misc]
        assert repr(params) == "OffsetParams(offset=40, limit=20)"  # part of the cache key
        assert hash(params) == hash(OffsetParams(offset=40, limit=20))

    def test_none_outside_a_request(self) -> None:
        offsets = PaginationOffset()
        params = offsets.params(offset=5)
        assert params.links is None
        assert offsets.paginate(ITEMS, params).offset == 5


app_offsets = PaginationOffset(default_limit=5, max_limit=10)


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
        params: Annotated[OffsetParams, Depends(app_offsets)],
        q: str | None = None,
    ) -> OffsetPage[Item]:
        calls.append("items")
        found = [item for item in ITEMS if q is None or str(item.id).startswith(q)]
        rows = found[params.offset : params.offset + params.fetch]  # LIMIT :fetch OFFSET :offset
        return app_offsets.paginate(rows, params, queried=True)

    return TestClient(app)


def links(response: httpx.Response) -> dict[str, str]:
    """The Link header as a client reads it: relation -> target."""
    return {rel: link["url"] for rel, link in response.links.items() if rel is not None}


class TestInAnApp:
    def test_each_page_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        assert ids(client.get("/items").json()) == [0, 1, 2, 3, 4]
        second = client.get("/items?offset=5")
        assert (served(second.headers), ids(second.json())) == ("miss", [5, 6, 7, 8, 9])
        again = client.get("/items?offset=5")
        assert (served(again.headers), ids(again.json())) == ("hit", [5, 6, 7, 8, 9])
        assert ids(client.get("/items?offset=5&limit=2").json()) == [5, 6]
        assert len(calls) == 3

    def test_the_page_as_clients_see_it(self, client: TestClient) -> None:
        body = client.get("/items?offset=20&limit=10").json()
        assert (ids(body), body["has_more"]) == ([20, 21, 22, 23, 24], False)
        assert body["total"] is None  # nothing counted

    @pytest.mark.parametrize(
        "path",
        ["/items?limit=11", "/items?limit=0", "/items?offset=-1", "/items?offset=many"],
    )
    def test_wrong_parameters_never_reach_the_endpoint(
        self,
        client: TestClient,
        calls: list[str],
        path: str,
    ) -> None:
        assert client.get(path).status_code == 422
        assert calls == []

    def test_the_parameters_are_documented(self, client: TestClient) -> None:
        operation = client.get("/openapi.json").json()["paths"]["/items"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        assert set(parameters) == {"offset", "limit", "q"}
        limit = parameters["limit"]["schema"]
        assert (limit["maximum"], limit["default"]) == (10, 5)  # with this tool's limits

    def test_links(self, client: TestClient) -> None:
        response = client.get("/items?offset=7&limit=5")
        assert response.headers["Link"] == (
            '</items?offset=0&limit=5>; rel="first", </items?offset=2&limit=5>; rel="prev", '
            '</items?offset=12&limit=5>; rel="next"'
        )
        assert links(client.get("/items?offset=3")) == {
            "first": "/items?offset=0&limit=5",
            "prev": "/items?offset=0&limit=5",
            "next": "/items?offset=8&limit=5",
        }

    def test_no_next_nor_last_at_the_end(self, client: TestClient) -> None:
        assert links(client.get("/items?offset=20")) == {
            "first": "/items?offset=0&limit=5",
            "prev": "/items?offset=15&limit=5",
        }
        assert links(client.get("/items?q=99")) == {"first": "/items?q=99&offset=0&limit=5"}

    def test_hits_get_the_links_of_their_own_request(
        self,
        client: TestClient,
        calls: list[str],
    ) -> None:
        first = client.get("/items?offset=5&ref=mail")
        again = client.get("/items?offset=5&ref=feed")
        assert (served(again.headers), len(calls)) == ("hit", 1)
        assert links(first)["next"] == "/items?ref=mail&offset=10&limit=5"
        assert links(again)["next"] == "/items?ref=feed&offset=10&limit=5"
