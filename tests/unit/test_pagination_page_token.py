"""PaginationToken: signed page tokens, bound to the query they were issued for, that expire."""

import base64
import json
import time
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.page_token import PaginationToken, TokenPage, TokenParams
from fastapi_alcazaba.pagination.values import Sealer
from fastapi_alcazaba.response_cache import cache
from tests.cache_status import served

SECRET = "s" * 32


class Item(BaseModel):
    id: int


ITEMS = [Item(id=i) for i in range(25)]


def item_ids(page: TokenPage[Item]) -> list[int]:
    return [item.id for item in page.items]


def refused(tokens: PaginationToken, **params: Any) -> str:
    """The detail of the 400 that `tokens.params(**params)` raises."""
    with pytest.raises(HTTPException) as error:
        tokens.params(**params)
    assert error.value.status_code == 400
    return str(error.value.detail)


class TestPages:
    def test_walking_a_list(self) -> None:
        tokens, seen, token = PaginationToken(SECRET), [], None
        while True:
            page = tokens.paginate(ITEMS, tokens.params(page_token=token, page_size=10))
            seen.append(item_ids(page))
            if (token := page.next_page_token) is None:
                break
        assert seen == [list(range(10)), list(range(10, 20)), list(range(20, 25))]

    def test_the_page_size_may_change_from_one_page_to_the_next(self) -> None:
        tokens = PaginationToken(SECRET)
        first = tokens.paginate(ITEMS, tokens.params(page_size=4))
        params = tokens.params(page_token=first.next_page_token, page_size=2)
        assert (params.offset, item_ids(tokens.paginate(ITEMS, params))) == (4, [4, 5])

    def test_rows_queried_from_a_database(self) -> None:
        tokens = PaginationToken(SECRET)
        params = tokens.params(page_size=10)
        assert (params.offset, params.fetch) == (0, 11)
        rows = ITEMS[params.offset : params.offset + params.fetch]  # LIMIT 11 OFFSET 0
        first = tokens.paginate(rows, params, queried=True)
        assert item_ids(first) == list(range(10))
        params = tokens.params(page_token=first.next_page_token, page_size=10)
        rows = ITEMS[params.offset : params.offset + params.fetch]
        second = tokens.paginate(rows, params, queried=True)
        assert item_ids(second) == list(range(10, 20))
        params = tokens.params(page_token=second.next_page_token, page_size=10)
        rows = ITEMS[params.offset : params.offset + params.fetch]
        last = tokens.paginate(rows, params, queried=True)
        assert (item_ids(last), last.next_page_token) == (list(range(20, 25)), None)

    def test_end_of_the_list(self) -> None:
        tokens = PaginationToken(SECRET)
        page = tokens.paginate(ITEMS, tokens.params(page_size=25))
        assert (len(page.items), page.next_page_token) == (25, None)
        assert tokens.paginate([], tokens.params()).model_dump() == {
            "items": [],
            "next_page_token": None,
        }

    def test_limits(self) -> None:
        tokens = PaginationToken(SECRET, default_page_size=10, max_page_size=20)
        assert tokens.params() == TokenParams(page_size=10)
        assert tokens.params(page_size=20).fetch == 21
        with pytest.raises(ValueError, match="over 20"):
            tokens.params(page_size=21)
        with pytest.raises(ValueError, match="default page_size"):
            PaginationToken(SECRET, default_page_size=30, max_page_size=20)
        with pytest.raises(ValueError, match="ttl"):
            PaginationToken(SECRET, ttl=0)
        for page_size, offset in ((0, 0), (10, -1)):
            with pytest.raises(ValueError, match="page_size starts at 1"):
                TokenParams(page_size=page_size, offset=offset)

    def test_params_are_immutable_and_stable(self) -> None:
        params = PaginationToken(SECRET).params(query=[("genre", "poetry")])
        with pytest.raises(AttributeError):
            params.offset = 3  # type: ignore[misc]
        # Part of the cache key: the query's digest keeps the pages of each query apart.
        assert repr(params) == (
            f"TokenParams(page_size=50, offset=0, page_token=None, "
            f"query_digest='{params.query_digest}')"
        )
        assert params != PaginationToken(SECRET).params(query=[("genre", "novel")])
        assert hash(params) == hash(PaginationToken(SECRET).params(query=[("genre", "poetry")]))


class TestBoundToTheQuery:
    def test_a_token_of_other_filters_is_a_400(self) -> None:
        tokens = PaginationToken(SECRET)
        params = tokens.params(page_size=5, query=[("genre", "poetry")])
        token = tokens.paginate(ITEMS, params).next_page_token
        assert tokens.params(page_token=token, query=[("genre", "poetry")]).offset == 5
        for other in ([("genre", "novel")], [], [("genre", "poetry"), ("author", "Lorca")]):
            detail = refused(tokens, page_token=token, query=other)
            assert detail == "page_token was issued for other query parameters"

    def test_order_and_the_page_parameters_dont_count(self) -> None:
        tokens = PaginationToken(SECRET)
        query = [("genre", "poetry"), ("tag", "a"), ("tag", "b")]
        token = tokens.paginate(ITEMS, tokens.params(page_size=5, query=query)).next_page_token
        assert token is not None
        same = [("tag", "b"), ("page_size", "7"), ("genre", "poetry"), ("page_token", token)]
        assert tokens.params(page_token=token, query=[*same, ("tag", "a")]).offset == 5

    def test_the_next_token_keeps_the_binding(self) -> None:
        tokens = PaginationToken(SECRET)
        query = [("genre", "poetry")]
        first = tokens.paginate(ITEMS, tokens.params(page_size=5, query=query))
        params = tokens.params(page_token=first.next_page_token, page_size=5, query=query)
        second = tokens.paginate(ITEMS, params)
        assert tokens.params(page_token=second.next_page_token, query=query).offset == 10
        assert refused(tokens, page_token=second.next_page_token).startswith("page_token was")


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1_800_000_000.0
        monkeypatch.setattr(time, "time", lambda: self.now)


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


class TestExpiry:
    def test_a_token_lasts_ttl_seconds(self, clock: Clock) -> None:
        tokens = PaginationToken(SECRET, ttl=600)
        token = tokens.paginate(ITEMS, tokens.params(page_size=5)).next_page_token
        clock.now += 600
        assert tokens.params(page_token=token).offset == 5
        clock.now += 1
        assert refused(tokens, page_token=token) == "Expired page_token"

    def test_from_when_its_page_was_made(self, clock: Clock) -> None:
        tokens = PaginationToken(SECRET, ttl=600)
        first = tokens.paginate(ITEMS, tokens.params(page_size=5))
        clock.now += 500
        params = tokens.params(page_token=first.next_page_token, page_size=5)
        second = tokens.paginate(ITEMS, params)
        clock.now += 500  # the first token has expired, the second one hasn't
        assert refused(tokens, page_token=first.next_page_token) == "Expired page_token"
        assert tokens.params(page_token=second.next_page_token).offset == 10


def raw(payload: object) -> str:
    """An unsigned token holding `payload`, as a client could write it."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


class TestTampering:
    def test_tokens_that_were_made_up_or_changed(self) -> None:
        tokens = PaginationToken(SECRET)
        token = tokens.paginate(ITEMS, tokens.params(page_size=5)).next_page_token
        assert token is not None
        payload, tag = token.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        offset, digest, issued = json.loads(base64.urlsafe_b64decode(padded))
        assert offset == 5  # readable, not secret
        for forged in (
            raw([20, digest, issued]),
            f"{raw([20, digest, issued])}.{tag}",
            payload,
            f"{payload}.{'A' * 22}",
            f"{payload}.{tag[:-1]}é",
            "x" * 1025,
            "not-a-token",
        ):
            assert refused(tokens, page_token=forged) == "Invalid page_token"

    def test_signed_by_another_secret_or_for_another_purpose(self) -> None:
        token = PaginationToken("o" * 32).paginate(ITEMS, TokenParams(page_size=5)).next_page_token
        assert refused(PaginationToken(SECRET), page_token=token) == "Invalid page_token"
        cursor = Sealer(SECRET, b"fastapi_alcazaba.cursor").seal([5, "x", int(time.time())])
        assert refused(PaginationToken(SECRET), page_token=cursor) == "Invalid page_token"

    @pytest.mark.parametrize(
        "payload",
        [
            [-1, "d", 0],
            ["5", "d", 0],
            [True, "d", 0],
            [5, "d", True],
            [5, 1, 0],
            [5, "d"],
            [5, "d", 0, 0],
            {"offset": 5},
            5,
            None,
        ],
    )
    def test_signed_but_not_a_page_token(self, payload: object) -> None:
        token = Sealer(SECRET, b"fastapi_alcazaba.page_token").seal(payload)
        assert refused(PaginationToken(SECRET), page_token=token) == "Invalid page_token"


class TestRotatingTheSecret:
    def test_old_tokens_are_accepted_new_ones_signed_with_the_new_secret(self) -> None:
        old = PaginationToken("o" * 32)
        token = old.paginate(ITEMS, old.params(page_size=5)).next_page_token
        rotated = PaginationToken(["n" * 32, "o" * 32])
        assert rotated.params(page_token=token).offset == 5
        new = rotated.paginate(ITEMS, rotated.params(page_size=5)).next_page_token
        assert PaginationToken("n" * 32).params(page_token=new).offset == 5
        assert refused(old, page_token=new) == "Invalid page_token"

    def test_a_secret_is_needed_and_long_enough(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            PaginationToken("k" * 31)
        with pytest.raises(ValueError, match="at least one key"):
            PaginationToken([])


app_tokens = PaginationToken(SECRET, default_page_size=4, max_page_size=10)


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
        params: Annotated[TokenParams, Depends(app_tokens)],
        q: str | None = None,
    ) -> TokenPage[Item]:
        calls.append("items")
        found = [item for item in ITEMS if q is None or str(item.id).startswith(q)]
        return app_tokens.paginate(found, params)

    return TestClient(app)


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


def links(response: httpx.Response) -> dict[str, str]:
    return {rel: link["url"] for rel, link in response.links.items() if rel is not None}


class TestInAnApp:
    def test_walking_the_pages(self, client: TestClient) -> None:
        first = client.get("/items?q=1&page_size=3").json()
        assert ids(first) == [1, 10, 11]
        second = client.get(f"/items?q=1&page_size=3&page_token={first['next_page_token']}")
        assert ids(second.json()) == [12, 13, 14]
        third = client.get(links(second)["next"]).json()
        assert ids(third) == [15, 16, 17]
        last = client.get(f"/items?q=1&page_size=3&page_token={third['next_page_token']}")
        assert (ids(last.json()), last.json()["next_page_token"]) == ([18, 19], None)
        assert "link" not in last.headers

    def test_the_next_link_keeps_the_query(self, client: TestClient) -> None:
        response = client.get("/items?q=1&page_size=3")
        token = response.json()["next_page_token"]
        assert links(response) == {"next": f"/items?q=1&page_size=3&page_token={token}"}
        default = client.get("/items")
        token = default.json()["next_page_token"]
        assert links(default) == {"next": f"/items?page_token={token}"}

    def test_a_token_of_another_query_never_reaches_the_endpoint(
        self,
        client: TestClient,
        calls: list[str],
    ) -> None:
        token = client.get("/items?q=1").json()["next_page_token"]
        for other in ("q=2&", "", "q=1&ref=mail&"):
            response = client.get(f"/items?{other}page_token={token}")
            assert (response.status_code, response.json()) == (
                400,
                {"detail": "page_token was issued for other query parameters"},
            )
        assert client.get(f"/items?page_token={token}&page_size=9&q=1").status_code == 200
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/items?page_size=0", 422),
            ("/items?page_size=11", 422),  # this tool's maximum
            ("/items?page_size=lots", 422),
            ("/items?page_token=has%20spaces", 422),
            ("/items?page_token=a.b.c", 422),
            (f"/items?page_token={'x' * 2000}", 422),
            (f"/items?page_token={raw([4, 'd', 0])}", 400),  # not signed
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
        assert set(parameters) == {"page_token", "page_size", "q"}
        size = parameters["page_size"]["schema"]
        assert (size["maximum"], size["default"]) == (10, 4)


class TestWithTheCache:
    def test_each_page_has_its_entry(self, client: TestClient, calls: list[str]) -> None:
        first = client.get("/items")
        path = f"/items?page_token={first.json()['next_page_token']}"
        assert (served(client.get(path).headers), len(calls)) == ("miss", 2)
        hit = client.get(path)
        assert (served(hit.headers), ids(hit.json())) == ("hit", [4, 5, 6, 7])
        assert links(hit) == {"next": f"/items?page_token={hit.json()['next_page_token']}"}
        assert len(calls) == 2

    def test_each_query_has_its_own_pages_and_tokens(
        self,
        client: TestClient,
        calls: list[str],
    ) -> None:
        """A parameter that the endpoint doesn't read still binds the token: a page cached for
        one query never hands its token to another, where it would be refused."""
        mail = client.get("/items?ref=mail")
        feed = client.get("/items?ref=feed")
        assert (served(feed.headers), len(calls)) == ("miss", 2)
        for ref, page in (("mail", mail), ("feed", feed)):
            token = page.json()["next_page_token"]
            assert ids(client.get(f"/items?ref={ref}&page_token={token}").json()) == [4, 5, 6, 7]
        assert served(client.get("/items?ref=mail").headers) == "hit"
