"""The page number example: a blog's archive, numbered pages with the total, cached by tag."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import page_number
from fastapi_alcazaba import FastAPICache


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(page_number, "db", page_number.Database())
    with TestClient(page_number.app) as client:
        yield client
    FastAPICache.reset()


def ids(body: dict[str, Any]) -> list[int]:
    return [post["id"] for post in body["items"]]


class TestPageNumberExample:
    def test_numbered_pages_newest_first(self, client: TestClient) -> None:
        body = client.get("/posts?page=2&page_size=10").json()
        assert ids(body) == list(range(35, 25, -1))
        assert {k: v for k, v in body.items() if k != "items"} == {
            "total": 45,
            "page": 2,
            "page_size": 10,
            "pages": 5,
        }

    def test_the_links(self, client: TestClient) -> None:
        links = client.get("/posts?page=2").links
        assert links["prev"]["url"] == "/posts?page=1&page_size=10"
        assert links["next"]["url"] == "/posts?page=3&page_size=10"
        assert links["last"]["url"] == "/posts?page=5&page_size=10"

    def test_each_page_cached_apart(self, client: TestClient) -> None:
        client.get("/posts?page=2")
        queries = page_number.db.queries
        assert ids(client.get("/posts?page=2").json())[0] == 35
        assert page_number.db.queries == queries  # from the cache
        client.get("/posts?page=3")
        assert page_number.db.queries == queries + 2  # its own entry: the page and the count

    def test_publishing_drops_every_page(self, client: TestClient) -> None:
        assert ids(client.get("/posts").json())[0] == 45
        assert client.post("/posts", json={"title": "New"}).status_code == 201
        body = client.get("/posts").json()
        assert (ids(body)[0], body["total"]) == (46, 46)

    def test_the_limit(self, client: TestClient) -> None:
        assert client.get("/posts?page_size=21").status_code == 422
        assert client.get("/posts?page=0").status_code == 422
