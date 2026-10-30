"""The keyset example: olive oils by price, browsed both ways."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination.keyset import Database, app
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


class TestBothWays:
    def test_forwards_and_back(self, client: TestClient) -> None:
        first = client.get("/products").json()
        assert (ids(first), first["before"]) == ([7, 4, 12], None)
        second = client.get(f"/products?after={first['after']}").json()
        assert ids(second) == [6, 1, 3]
        third = client.get(f"/products?after={second['after']}").json()
        assert ids(third) == [10, 5, 2]  # 9.90 is shared by 2 and 9, on two pages
        last = client.get(f"/products?after={third['after']}").json()
        assert (ids(last), last["after"]) == ([9, 8, 11], None)
        back = client.get(f"/products?before={last['before']}").json()
        assert ids(back) == [10, 5, 2]
        back = client.get(f"/products?before={back['before']}").json()
        back = client.get(f"/products?before={back['before']}").json()
        assert (ids(back), back["before"]) == ([7, 4, 12], None)
        assert queries() == 7

    def test_through_the_links(self, client: TestClient) -> None:
        first = client.get("/products?limit=5")
        assert set(first.links) == {"first", "next"}
        second = client.get(first.links["next"]["url"])
        assert ids(second.json()) == [3, 10, 5, 2, 9]
        assert set(second.links) == {"first", "prev", "next"}
        assert ids(client.get(second.links["prev"]["url"]).json()) == [7, 4, 12, 6, 1]

    def test_each_page_has_its_entry(self, client: TestClient) -> None:
        key = client.get("/products").json()["after"]
        assert served(client.get(f"/products?after={key}").headers) == "miss"
        hit = client.get(f"/products?after={key}")
        assert (served(hit.headers), ids(hit.json())) == ("hit", [6, 1, 3])
        assert queries() == 2


class TestWrites:
    def test_a_new_product_doesnt_shift_the_pages(self, client: TestClient) -> None:
        first = client.get("/products").json()
        added = client.post("/products", json={"name": "Picual 250 ml", "price": 3.5})
        assert (added.status_code, added.json()["id"]) == (201, 13)
        assert ids(client.get(f"/products?after={first['after']}").json()) == [6, 1, 3]
        assert ids(client.get("/products").json()) == [13, 7, 4]


class TestRefused:
    def test_made_up_keys(self, client: TestClient) -> None:
        response = client.get("/products?after=WzMuOSw3XQ")  # [3.9,7], not signed
        assert (response.status_code, response.json()) == (400, {"detail": "Invalid after"})

    def test_both_ways_at_once(self, client: TestClient) -> None:
        key = client.get("/products").json()["after"]
        response = client.get(f"/products?after={key}&before={key}")
        assert response.status_code == 400
        assert queries() == 1

    def test_pages_are_at_most_50_items(self, client: TestClient) -> None:
        assert client.get("/products?limit=51").status_code == 422
        assert queries() == 0
