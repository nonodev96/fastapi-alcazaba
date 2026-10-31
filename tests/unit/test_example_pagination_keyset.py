"""The keyset example: a chat's messages after the last one seen, or back from the newest."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import keyset
from fastapi_alcazaba import FastAPICache
from tests.cache_status import served


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(keyset.app) as client:
        yield client
    FastAPICache.reset()


def queries() -> int:
    db: keyset.Database = keyset.app.state.db
    return db.queries


def ids(body: dict[str, Any]) -> list[int]:
    return [message["id"] for message in body["items"]]


class TestKeysetExample:
    def test_after_the_last_message_seen(self, client: TestClient) -> None:
        first = client.get("/messages?limit=3").json()
        assert (ids(first), first["after"], first["has_more"]) == ([1, 2, 3], "3", True)
        second = client.get(f"/messages?after={first['after']}&limit=3").json()
        assert ids(second) == [4, 5, 6]

    def test_the_last_page(self, client: TestClient) -> None:
        response = client.get("/messages?after=58")
        body = response.json()
        assert (ids(body), body["after"], body["has_more"]) == ([59, 60], "60", False)
        assert set(response.links) == {"first"}

    def test_newest_first(self, client: TestClient) -> None:
        first = client.get("/messages/newest?limit=3")
        assert ids(first.json()) == [60, 59, 58]
        assert first.links["next"]["url"] == "/messages/newest?after=58&limit=3"
        assert ids(client.get("/messages/newest?after=58&limit=3").json()) == [57, 56, 55]
        last = client.get("/messages/newest?after=3").json()
        assert (ids(last), last["has_more"]) == ([2, 1], False)

    def test_new_messages_reach_clients_that_sync(self, client: TestClient) -> None:
        caught_up = client.get("/messages?after=60").json()
        assert (caught_up["items"], caught_up["after"]) == ([], None)
        message = {"author": "ana", "text": "¿Comemos?"}
        assert client.post("/messages", json=message).json()["id"] == 61
        response = client.get("/messages?after=60")
        assert (served(response.headers), ids(response.json())) == ("miss", [61])
        assert ids(client.get("/messages?after=58&limit=2").json()) == [59, 60]

    def test_each_page_is_cached(self, client: TestClient) -> None:
        client.get("/messages?after=20")
        hit = client.get("/messages?after=20")
        assert (served(hit.headers), ids(hit.json())[0]) == ("hit", 21)
        assert hit.links["next"]["url"] == "/messages?after=40&limit=20"
        assert queries() == 1

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/messages?after=abc", 400),
            ("/messages/newest?after=1.5", 400),
            ("/messages?limit=101", 422),
        ],
    )
    def test_wrong_parameters(self, client: TestClient, path: str, status: int) -> None:
        response = client.get(path)
        assert response.status_code == status
        assert queries() == 0
