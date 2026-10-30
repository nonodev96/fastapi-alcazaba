"""The cursor example: an activity feed, newest first, whose pages new events don't shift."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import cursor
from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.pagination.cursor import PaginationCursor


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(cursor, "db", cursor.Database())
    with TestClient(cursor.app) as client:
        yield client
    FastAPICache.reset()


def ids(body: dict[str, Any]) -> list[int]:
    return [event["id"] for event in body["items"]]


class TestCursorExample:
    def test_page_after_page(self, client: TestClient) -> None:
        seen, url = [], "/events?limit=10"
        while True:
            body = client.get(url).json()
            seen += ids(body)
            if body["next_cursor"] is None:
                break
            url = f"/events?limit=10&cursor={body['next_cursor']}"
        assert seen == list(range(30, 0, -1))

    def test_new_events_dont_shift_the_next_pages(self, client: TestClient) -> None:
        first = client.get("/events").json()
        assert ids(first) == [30, 29, 28, 27, 26]
        for text in ("one", "two"):
            client.post("/events", json={"text": text})
        second = client.get(f"/events?cursor={first['next_cursor']}").json()
        assert ids(second) == [25, 24, 23, 22, 21]  # nothing seen twice

    def test_the_links(self, client: TestClient) -> None:
        response = client.get("/events?limit=5")
        next_cursor = response.json()["next_cursor"]
        assert response.links["next"]["url"] == f"/events?cursor={next_cursor}&limit=5"

    def test_made_up_cursors_are_refused(self, client: TestClient) -> None:
        cursor_of_mine = PaginationCursor().encode(("2026-10-29", 1))
        assert client.get(f"/events?cursor={cursor_of_mine}").status_code == 400
        assert client.get("/events?limit=21").status_code == 422
