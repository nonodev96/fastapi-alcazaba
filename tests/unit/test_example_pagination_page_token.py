"""The page token example: books paginated by tokens bound to the query, and cached page by page."""

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination.page_token import Database, app
from fastapi_alcazaba import FastAPICache
from tests.cache_status import served


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client
    FastAPICache.reset()


def queries() -> int:
    db: Database = app.state.db
    return db.queries


def ids(body: dict[str, Any]) -> list[int]:
    return [book["id"] for book in body["items"]]


class TestPages:
    def test_walking_a_genre(self, client: TestClient) -> None:
        url, seen, token = "/books?genre=poetry&page_size=4", [], None
        while True:
            body = client.get(url if token is None else f"{url}&page_token={token}").json()
            seen.append(ids(body))
            if (token := body["next_page_token"]) is None:
                break
        assert seen == [[3, 6, 9, 12], [15, 18, 21, 24], [27, 30]]
        assert queries() == 3

    def test_the_next_link(self, client: TestClient) -> None:
        first = client.get("/books?genre=novel&page_size=3")
        token = first.json()["next_page_token"]
        assert first.links["next"]["url"] == f"/books?genre=novel&page_size=3&page_token={token}"
        assert ids(client.get(first.links["next"]["url"]).json()) == [10, 13, 16]

    def test_each_page_is_cached(self, client: TestClient) -> None:
        first = client.get("/books?page_size=5")
        again = client.get("/books?page_size=5")
        assert (served(first.headers), served(again.headers)) == ("miss", "hit")
        assert again.json()["next_page_token"] == first.json()["next_page_token"]
        assert "next" in again.links
        assert queries() == 1


class TestRefused:
    def test_a_token_with_another_genre(self, client: TestClient) -> None:
        token = client.get("/books?genre=poetry&page_size=4").json()["next_page_token"]
        response = client.get(f"/books?genre=novel&page_size=4&page_token={token}")
        assert (response.status_code, response.json()) == (
            400,
            {"detail": "page_token was issued for other query parameters"},
        )
        assert queries() == 1

    def test_a_made_up_token(self, client: TestClient) -> None:
        response = client.get("/books?page_token=MTA")
        assert (response.status_code, response.json()) == (400, {"detail": "Invalid page_token"})

    def test_a_token_older_than_ten_minutes(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = client.get("/books?page_size=4").json()["next_page_token"]
        later = time.time() + 601
        monkeypatch.setattr(time, "time", lambda: later)
        response = client.get(f"/books?page_size=4&page_token={token}")
        assert (response.status_code, response.json()) == (400, {"detail": "Expired page_token"})

    def test_pages_are_at_most_50_books(self, client: TestClient) -> None:
        assert client.get("/books?page_size=51").status_code == 422
        assert queries() == 0
