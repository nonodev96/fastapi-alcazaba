"""PaginationIdBased: the items after the last id seen, in id order or newest first."""

from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.id_based import IdBasedPage, IdBasedParams, PaginationIdBased
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import served


class Message(BaseModel):
    id: int


MESSAGES = [Message(id=i) for i in range(1, 12)]


def by_id(message: Message) -> int:
    return message.id


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def message_ids(page: IdBasedPage[Message]) -> list[int]:
    return [message.id for message in page.items]


class TestById:
    def test_walking_a_list(self) -> None:
        pages, seen, last_id = PaginationIdBased(), [], None
        while True:
            page = pages.paginate(MESSAGES, pages.params(last_id=last_id, limit=4), key=by_id)
            seen.append(message_ids(page))
            if not page.has_more:
                break
            last_id = page.last_id
        assert seen == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11]]
        assert page.model_dump() == {
            "items": [{"id": 9}, {"id": 10}, {"id": 11}],
            "limit": 4,
            "last_id": 11,
            "has_more": False,
        }

    def test_newest_first(self) -> None:
        pages, newest = PaginationIdBased(), MESSAGES[::-1]
        first = pages.paginate(newest, pages.params(limit=5), key=by_id, descending=True)
        params = pages.params(last_id=first.last_id, limit=5)
        second = pages.paginate(newest, params, key=by_id, descending=True)
        assert (message_ids(first), message_ids(second)) == ([11, 10, 9, 8, 7], [6, 5, 4, 3, 2])

    def test_rows_added_or_deleted_meanwhile_dont_shift_the_pages(self) -> None:
        pages = PaginationIdBased()
        first = pages.paginate(MESSAGES, pages.params(limit=4), key=by_id)
        changed = [m for m in MESSAGES if m.id not in (2, 5)] + [Message(id=12)]
        params = pages.params(last_id=first.last_id, limit=4)
        assert message_ids(pages.paginate(changed, params, key=by_id)) == [6, 7, 8, 9]

    def test_an_id_that_isnt_there_any_more(self) -> None:
        pages = PaginationIdBased()
        without_4 = [m for m in MESSAGES if m.id != 4]
        page = pages.paginate(without_4, pages.params(last_id=4, limit=2), key=by_id)
        assert message_ids(page) == [5, 6]

    def test_rows_queried_from_a_database(self) -> None:
        pages = PaginationIdBased()
        params = pages.params(limit=3)
        assert (params.last_id, params.fetch) == (None, 4)
        first = pages.paginate(MESSAGES[: params.fetch], params, key=by_id, queried=True)
        assert (message_ids(first), first.last_id, first.has_more) == ([1, 2, 3], 3, True)
        params = pages.params(last_id=first.last_id, limit=3)
        last_id = params.last_id
        assert isinstance(last_id, int)
        rows = [m for m in MESSAGES if m.id > last_id][: params.fetch]  # WHERE id > 3 LIMIT 4
        assert message_ids(pages.paginate(rows, params, key=by_id, queried=True)) == [4, 5, 6]
        last = pages.paginate(MESSAGES[9:], params, key=by_id, queried=True)
        assert (message_ids(last), last.last_id, last.has_more) == ([10, 11], 11, False)

    def test_end_of_the_list(self) -> None:
        pages = PaginationIdBased()
        assert pages.paginate(MESSAGES, pages.params(last_id=11), key=by_id).items == []
        assert pages.paginate([], pages.params(), key=by_id).model_dump() == {
            "items": [],
            "limit": 50,
            "last_id": None,
            "has_more": False,
        }

    def test_string_and_uuid_ids(self) -> None:
        names = sorted(["ana", "bea", "eva", "ivo", "leo"])
        by_name = PaginationIdBased(str)
        page = by_name.paginate(names, by_name.params(last_id="bea", limit=2), key=str)
        assert (page.items, page.last_id, page.has_more) == (["eva", "ivo"], "ivo", True)
        keys = sorted(uuid4() for _ in range(5))
        by_uuid = PaginationIdBased(UUID)
        params = by_uuid.params(last_id=keys[2], limit=2)
        page_of_keys = by_uuid.paginate(keys, params, key=lambda value: value)
        assert (page_of_keys.items, page_of_keys.has_more) == (keys[3:], False)

    def test_the_id_type(self) -> None:
        with pytest.raises(ValueError, match="int, str or UUID"):
            PaginationIdBased(float)  # type: ignore[arg-type]
        for pages, wrong in (
            (PaginationIdBased(), "4"),
            (PaginationIdBased(), True),
            (PaginationIdBased(str), 4),
            (PaginationIdBased(UUID), str(uuid4())),
        ):
            with pytest.raises(ValueError, match="last_id is a"):
                pages.params(last_id=wrong)

    def test_limits(self) -> None:
        pages = PaginationIdBased(default_limit=10, max_limit=20)
        assert pages.params() == IdBasedParams(last_id=None, limit=10)
        with pytest.raises(ValueError, match="over 20"):
            pages.params(limit=21)
        with pytest.raises(ValueError, match="default limit"):
            PaginationIdBased(default_limit=0)

    def test_params_are_checked_immutable_and_stable(self) -> None:
        with pytest.raises(ValueError, match="limit starts at 1"):
            IdBasedParams(limit=0)
        params = IdBasedParams(last_id=42, limit=20)
        with pytest.raises(AttributeError):
            params.limit = 30  # type: ignore[misc]
        assert repr(params) == "IdBasedParams(last_id=42, limit=20)"  # part of the cache key
        assert repr(IdBasedParams(last_id="42", limit=20)) != repr(params)
        assert hash(params) == hash(IdBasedParams(last_id=42, limit=20))

    def test_none_outside_a_request(self) -> None:
        pages = PaginationIdBased()
        params = pages.params(last_id=5)
        assert params.links is None
        assert message_ids(pages.paginate(MESSAGES, params, key=by_id))[0] == 6


app_ids = PaginationIdBased(default_limit=4, max_limit=10)
app_uuids = PaginationIdBased(UUID)
KEYS = sorted(uuid4() for _ in range(3))


@pytest.fixture(name="calls")
def fixture_calls() -> list[str]:
    return []


@pytest.fixture(name="client")
def fixture_client(calls: list[str]) -> TestClient:
    FastAPICache.init(InMemoryBackend())
    app = FastAPI()

    @app.get("/messages")
    @cache(expire=60)
    async def messages(
        params: Annotated[IdBasedParams, Depends(app_ids)],
        q: str | None = None,
    ) -> IdBasedPage[Message]:
        calls.append("messages")
        found = [m for m in MESSAGES if q is None or str(m.id).startswith(q)]
        return app_ids.paginate(found, params, key=by_id)

    @app.get("/keys")
    async def keys(params: Annotated[IdBasedParams, Depends(app_uuids)]) -> IdBasedPage[UUID]:
        calls.append("keys")
        return app_uuids.paginate(KEYS, params, key=lambda value: value)

    return TestClient(app)


def links(response: httpx.Response) -> dict[str, str]:
    """The Link header as a client reads it: relation -> target."""
    return {rel: link["url"] for rel, link in response.links.items() if rel is not None}


class TestInAnApp:
    def test_each_page_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        assert ids(client.get("/messages").json()) == [1, 2, 3, 4]
        second = client.get("/messages?last_id=4")
        assert (served(second.headers), ids(second.json())) == ("miss", [5, 6, 7, 8])
        again = client.get("/messages?last_id=4")
        assert (served(again.headers), ids(again.json())) == ("hit", [5, 6, 7, 8])
        assert ids(client.get("/messages?last_id=4&limit=2").json()) == [5, 6]
        assert len(calls) == 3

    def test_uuids(self, client: TestClient) -> None:
        first = client.get("/keys?limit=2").json()
        assert first == {
            "items": [str(KEYS[0]), str(KEYS[1])],
            "limit": 2,
            "last_id": str(KEYS[1]),
            "has_more": True,
        }
        second = client.get(f"/keys?last_id={first['last_id']}&limit=2").json()
        assert (second["items"], second["has_more"]) == ([str(KEYS[2])], False)

    @pytest.mark.parametrize(
        "path",
        [
            "/messages?limit=11",
            "/messages?limit=0",
            "/messages?last_id=four",
            "/messages?last_id=1.5",
            f"/messages?last_id={2**63}",
            f"/messages?last_id={-(2**63) - 1}",
            "/keys?last_id=42",
        ],
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
        paths = client.get("/openapi.json").json()["paths"]
        parameters = {p["name"]: p for p in paths["/messages"]["get"]["parameters"]}
        assert set(parameters) == {"last_id", "limit", "q"}
        last_id = parameters["last_id"]["schema"]["anyOf"][0]
        assert (last_id["type"], last_id["maximum"]) == ("integer", 2**63 - 1)
        limit = parameters["limit"]["schema"]
        assert (limit["maximum"], limit["default"]) == (10, 4)  # with this tool's limits
        key = paths["/keys"]["get"]["parameters"][0]["schema"]["anyOf"][0]
        assert (key["type"], key["format"]) == ("string", "uuid")

    def test_links(self, client: TestClient) -> None:
        response = client.get("/messages?last_id=2")
        assert response.headers["Link"] == (
            '</messages?limit=4>; rel="first", </messages?last_id=6&limit=4>; rel="next"'
        )
        assert links(client.get("/messages?last_id=8")) == {"first": "/messages?limit=4"}
        assert links(client.get("/messages?q=99")) == {"first": "/messages?q=99&limit=4"}

    def test_hits_get_the_links_of_their_own_request(
        self,
        client: TestClient,
        calls: list[str],
    ) -> None:
        first = client.get("/messages?last_id=2&ref=mail")
        again = client.get("/messages?last_id=2&ref=feed")
        assert (served(again.headers), len(calls)) == ("hit", 1)
        assert links(first)["next"] == "/messages?ref=mail&last_id=6&limit=4"
        assert links(again)["next"] == "/messages?ref=feed&last_id=6&limit=4"
