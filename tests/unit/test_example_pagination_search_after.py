"""The search_after example: an activity log read after the sort values of the last event."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination.search_after import Database, app
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


class TestInOrder:
    def test_the_whole_log(self, client: TestClient) -> None:
        seen: list[list[int]] = []
        params: dict[str, str] = {}
        while True:
            body = client.get("/events", params=params).json()
            seen.append(ids(body))
            if body["search_after"] is None:
                break
            params = {"search_after": json.dumps(body["search_after"])}
        assert seen == [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10]]
        assert queries() == 4

    def test_the_values_of_the_last_event(self, client: TestClient) -> None:
        response = client.get("/events")
        assert response.json()["search_after"] == ["2026-10-29T09:58:30Z", 3]
        second = client.get(response.links["next"]["url"])
        assert ids(second.json()) == [4, 5, 6]

    def test_values_a_client_wrote(self, client: TestClient) -> None:
        params = {"search_after": '["2026-10-29T10:00:00Z",0]', "limit": "5"}
        assert ids(client.get("/events", params=params).json()) == [5, 6, 7, 8, 9]

    def test_each_position_has_its_entry(self, client: TestClient) -> None:
        params = {"search_after": '["2026-10-29T09:58:30Z",3]'}
        assert served(client.get("/events", params=params).headers) == "miss"
        hit = client.get("/events", params=params)
        assert (served(hit.headers), ids(hit.json())) == ("hit", [4, 5, 6])
        assert queries() == 1


class TestRefused:
    @pytest.mark.parametrize(
        "search_after",
        [
            '["2026-10-29T10:00:00",0]',  # no time zone
            '["2026-10-29T10:00:00Z"]',  # one value of two
            '["2026-10-29T10:00:00Z","0"]',  # a string for the id
            "2026-10-29T10:00:00Z",  # not JSON
        ],
    )
    def test_values_that_dont_fit(self, client: TestClient, search_after: str) -> None:
        response = client.get("/events", params={"search_after": search_after})
        assert (response.status_code, response.json()) == (
            400,
            {"detail": "Invalid search_after"},
        )
        assert queries() == 0
