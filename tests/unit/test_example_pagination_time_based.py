"""The time-based example: a news feed, newest first, and the posts since a time."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination.time_based import Database, app
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
    return [item["id"] for item in body["items"]]


def walk(client: TestClient, url: str) -> list[list[int]]:
    """The ids of every page, following the Link header."""
    pages, response = [], client.get(url)
    while True:
        pages.append(ids(response.json()))
        if "next" not in response.links:
            return pages
        response = client.get(response.links["next"]["url"])


class TestTheFeed:
    def test_newest_first_without_splitting_a_time(self, client: TestClient) -> None:
        assert walk(client, "/posts") == [[9, 8, 7], [6], [5, 4, 3], [2, 1]]
        assert queries() == 4

    def test_the_timestamp_of_the_next_page(self, client: TestClient) -> None:
        first = client.get("/posts").json()
        assert first["next_timestamp"] == "2026-10-29T10:00:00Z"
        second = client.get("/posts", params={"timestamp": first["next_timestamp"]}).json()
        assert (ids(second), second["next_timestamp"]) == ([6], "2026-10-29T09:30:00Z")

    def test_bigger_pages(self, client: TestClient) -> None:
        assert walk(client, "/posts?limit=4") == [[9, 8, 7, 6], [5, 4, 3, 2], [1]]

    def test_a_page_too_small_for_the_posts_of_a_time(self, client: TestClient) -> None:
        response = client.get("/posts?timestamp=2026-10-29T09:30:00Z&limit=2")
        assert (response.status_code, response.json()) == (
            400,
            {"detail": "More than 2 posts share a time: ask for more"},
        )

    def test_one_entry_per_page(self, client: TestClient) -> None:
        url = "/posts?timestamp=2026-10-29T10:00:00Z"
        assert served(client.get(url).headers) == "miss"
        hit = client.get("/posts?timestamp=2026-10-29T11:00:00%2B01:00")  # the same instant
        assert (served(hit.headers), ids(hit.json())) == ("hit", [6])
        assert queries() == 1


class TestSyncing:
    def test_the_posts_since_a_time(self, client: TestClient) -> None:
        assert walk(client, "/posts/since?timestamp=2026-10-29T08:15:00Z") == [
            [2],  # 3, 4 and 5 share 09:00, and go on the next page together
            [3, 4, 5],
            [6, 7, 8],
            [9],
        ]

    def test_a_new_post(self, client: TestClient) -> None:
        last = "/posts/since?timestamp=2026-10-29T10:20:00Z"
        assert ids(client.get(last).json()) == []
        added = client.post("/posts", json={"title": "Open day at the mill"})
        assert (added.status_code, added.json()["id"]) == (201, 10)
        assert ids(client.get(last).json()) == [10]
        assert ids(client.get("/posts").json()) == [10, 9]

    def test_a_timestamp_needs_its_time_zone(self, client: TestClient) -> None:
        assert client.get("/posts/since?timestamp=2026-10-29T08:15:00").status_code == 422
        assert queries() == 0
