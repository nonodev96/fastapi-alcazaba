"""The offset example: an access log, newest first, read without counting it."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import offset
from fastapi_alcazaba import FastAPICache
from tests.cache_status import served


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(offset.app) as client:
        yield client
    FastAPICache.reset()


def queries() -> int:
    db: offset.Database = offset.app.state.db
    return db.queries


def ids(body: dict[str, Any]) -> list[int]:
    return [line["id"] for line in body["items"]]


class TestOffsetExample:
    def test_newest_first_without_a_total(self, client: TestClient) -> None:
        body = client.get("/logs?limit=3").json()
        assert (ids(body), body["has_more"], body["total"]) == ([1000, 999, 998], True, None)
        assert ids(client.get("/logs?offset=3&limit=3").json()) == [997, 996, 995]

    def test_the_last_page(self, client: TestClient) -> None:
        response = client.get("/logs?level=error&offset=18")
        body = response.json()
        assert (ids(body), body["has_more"]) == ([100, 50], False)
        assert set(response.links) == {"first", "prev"}
        assert response.links["prev"]["url"] == "/logs?level=error&offset=0&limit=20"

    def test_the_links_keep_the_level(self, client: TestClient) -> None:
        response = client.get("/logs?level=warning&limit=5&offset=5")
        assert {rel: link["url"] for rel, link in response.links.items()} == {
            "first": "/logs?level=warning&offset=0&limit=5",
            "prev": "/logs?level=warning&offset=0&limit=5",
            "next": "/logs?level=warning&offset=10&limit=5",
        }

    def test_each_page_is_cached(self, client: TestClient) -> None:
        assert served(client.get("/logs?offset=20").headers) == "miss"
        hit = client.get("/logs?offset=20")
        assert (served(hit.headers), ids(hit.json())[0]) == ("hit", 980)
        assert "next" in hit.links
        assert queries() == 1

    @pytest.mark.parametrize("path", ["/logs?limit=101", "/logs?offset=-1", "/logs?level=debug"])
    def test_wrong_parameters(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 422
        assert queries() == 0
