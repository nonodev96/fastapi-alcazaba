"""The range example: a contact list for a data grid, with Content-Range."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import ranges
from fastapi_alcazaba import FastAPICache
from tests.cache_status import served


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(ranges.app) as client:
        yield client
    FastAPICache.reset()


def queries() -> int:
    db: ranges.Database = ranges.app.state.db
    return db.queries


def ids(body: dict[str, Any]) -> list[int]:
    return [contact["id"] for contact in body["items"]]


class TestRangeExample:
    def test_the_first_range(self, client: TestClient) -> None:
        response = client.get("/contacts")
        body = response.json()
        assert response.headers["Content-Range"] == "items 0-24/319"
        assert (len(body["items"]), body["start"], body["end"], body["total"]) == (25, 0, 24, 319)
        assert body["items"][0] == {"name": "Contact 001", "email": "contact1@example.com", "id": 1}
        assert response.links["next"]["url"] == "/contacts?start=25&end=49"
        assert response.links["last"]["url"] == "/contacts?start=300&end=324"

    def test_the_last_range_is_shorter(self, client: TestClient) -> None:
        response = client.get("/contacts?start=300&end=324")
        assert response.headers["Content-Range"] == "items 300-318/319"
        assert ids(response.json()) == list(range(301, 320))

    def test_past_the_last_contact(self, client: TestClient) -> None:
        response = client.get("/contacts?start=319")
        assert (response.status_code, response.headers["Content-Range"]) == (416, "items */319")

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/contacts?start=10&end=5", 400),
            ("/contacts?start=0&end=100", 400),
            ("/contacts?start=-1", 422),
        ],
    )
    def test_wrong_ranges(self, client: TestClient, path: str, status: int) -> None:
        assert client.get(path).status_code == status
        assert queries() == 0

    def test_hits_get_their_content_range(self, client: TestClient) -> None:
        client.get("/contacts?start=25&end=49")
        hit = client.get("/contacts?start=25&end=49")
        assert (served(hit.headers), hit.headers["Content-Range"]) == ("hit", "items 25-49/319")
        assert queries() == 1

    def test_a_new_contact_drops_every_range(self, client: TestClient) -> None:
        client.get("/contacts?start=300&end=324")
        added = client.post("/contacts", json={"name": "Zoe", "email": "zoe@example.com"})
        assert (added.status_code, added.json()["id"]) == (201, 320)
        response = client.get("/contacts?start=300&end=324")
        assert (served(response.headers), response.headers["Content-Range"]) == (
            "miss",
            "items 300-319/320",
        )
