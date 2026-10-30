"""The id-based example: a chat's messages after the last id seen, or back from the newest."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import id_based
from fastapi_alcazaba import FastAPICache
from tests.cache_status import served


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(id_based.app) as client:
        yield client
    FastAPICache.reset()


def queries() -> int:
    db: id_based.Database = id_based.app.state.db
    return db.queries


def ids(body: dict[str, Any]) -> list[int]:
    return [message["id"] for message in body["items"]]


class TestIdBasedExample:
    def test_after_the_last_id_seen(self, client: TestClient) -> None:
        first = client.get("/messages?limit=3").json()
        assert (ids(first), first["last_id"], first["has_more"]) == ([1, 2, 3], 3, True)
        second = client.get(f"/messages?last_id={first['last_id']}&limit=3").json()
        assert ids(second) == [4, 5, 6]

    def test_the_last_page(self, client: TestClient) -> None:
        response = client.get("/messages?last_id=58")
        body = response.json()
        assert (ids(body), body["last_id"], body["has_more"]) == ([59, 60], 60, False)
        assert set(response.links) == {"first"}

    def test_newest_first(self, client: TestClient) -> None:
        first = client.get("/messages/newest?limit=3")
        assert ids(first.json()) == [60, 59, 58]
        assert first.links["next"]["url"] == "/messages/newest?last_id=58&limit=3"
        assert ids(client.get("/messages/newest?last_id=58&limit=3").json()) == [57, 56, 55]
        last = client.get("/messages/newest?last_id=3").json()
        assert (ids(last), last["has_more"]) == ([2, 1], False)

    def test_new_messages_reach_clients_that_sync(self, client: TestClient) -> None:
        caught_up = client.get("/messages?last_id=60").json()
        assert (caught_up["items"], caught_up["last_id"]) == ([], None)
        message = {"author": "ana", "text": "¿Comemos?"}
        assert client.post("/messages", json=message).json()["id"] == 61
        response = client.get("/messages?last_id=60")
        assert (served(response.headers), ids(response.json())) == ("miss", [61])
        assert ids(client.get("/messages?last_id=58&limit=2").json()) == [59, 60]

    def test_each_page_is_cached(self, client: TestClient) -> None:
        client.get("/messages?last_id=20")
        hit = client.get("/messages?last_id=20")
        assert (served(hit.headers), ids(hit.json())[0]) == ("hit", 21)
        assert hit.links["next"]["url"] == "/messages?last_id=40&limit=20"
        assert queries() == 1

    @pytest.mark.parametrize(
        "path",
        ["/messages?last_id=abc", "/messages?limit=101", "/messages/newest?last_id=1.5"],
    )
    def test_wrong_parameters(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 422
        assert queries() == 0
