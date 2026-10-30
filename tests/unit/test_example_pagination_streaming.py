"""The streaming example: an activity log sent as NDJSON, read by key a batch at a time."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination.streaming import Database, app


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def db() -> Database:
    database: Database = app.state.db
    return database


def lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines()]


class TestEveryEvent:
    def test_one_event_per_line(self, client: TestClient) -> None:
        response = client.get("/events")
        assert response.headers["content-type"] == "application/x-ndjson"
        events = lines(response.text)
        assert [event["id"] for event in events] == list(range(1, 251))
        assert events[0] == {"id": 1, "kind": "upload", "at": "2026-10-29T09:01:00Z"}
        assert db().queries == 3  # 100, 100 and 50: the short batch is the last one

    def test_of_a_kind(self, client: TestClient) -> None:
        events = lines(client.get("/events?kind=login").text)
        assert len(events) == 83
        assert {event["kind"] for event in events} == {"login"}
        assert db().queries == 1

    def test_unknown_kinds_are_refused(self, client: TestClient) -> None:
        assert client.get("/events?kind=anything").status_code == 422
        assert db().queries == 0

    def test_not_cached(self, client: TestClient) -> None:
        client.get("/events?kind=logout")
        client.get("/events?kind=logout")
        assert db().queries == 2


class TestLatest:
    def test_the_25_newest_and_the_cursor_closed(self, client: TestClient) -> None:
        events = lines(client.get("/events/latest").text)
        assert [event["id"] for event in events] == list(range(250, 225, -1))
        assert db().open_cursors == 0
