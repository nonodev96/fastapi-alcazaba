"""The scroll example: an export of orders that sees them as they were when it started."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import scroll
from fastapi_alcazaba import FastAPICache


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(scroll, "db", scroll.Database())
    with TestClient(scroll.app) as client:
        yield client
    FastAPICache.reset()


def ids(body: dict[str, Any]) -> list[int]:
    return [order["id"] for order in body["items"]]


class TestScrollExample:
    def test_batch_after_batch(self, client: TestClient) -> None:
        response = client.get("/orders/export")
        batches = [ids(response.json())]
        while "next" in response.links:
            response = client.get(response.links["next"]["url"])
            batches.append(ids(response.json()))
        assert batches == [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12]]
        assert response.json()["total"] == 12

    def test_orders_placed_meanwhile_dont_show(self, client: TestClient) -> None:
        first = client.get("/orders/export?batch_size=10").json()
        added = client.post("/orders", json={"customer": "late", "amount": 5})
        assert (added.status_code, added.json()["id"]) == (201, 13)
        rest = client.get(f"/orders/export?batch_size=10&scroll_id={first['scroll_id']}").json()
        assert (ids(rest), rest["total"], rest["scroll_id"]) == ([11, 12], 12, None)
        assert client.get("/orders/export?batch_size=10").json()["total"] == 13

    def test_a_retried_request_gets_the_same_batch(self, client: TestClient) -> None:
        scroll_id = client.get("/orders/export").json()["scroll_id"]
        url = f"/orders/export?scroll_id={scroll_id}"
        assert client.get(url).json() == client.get(url).json()

    def test_the_filter_is_the_start_s(self, client: TestClient) -> None:
        response = client.get("/orders/export?status=shipped&batch_size=2")
        assert ids(response.json()) == [3, 6]
        url = response.links["next"]["url"]
        assert url.startswith("/orders/export?status=shipped&batch_size=2&scroll_id=")
        assert ids(client.get(url).json()) == [9, 12]

    def test_stopping_an_export(self, client: TestClient) -> None:
        scroll_id = client.get("/orders/export").json()["scroll_id"]
        assert client.delete(f"/orders/export?scroll_id={scroll_id}").status_code == 204
        assert client.get(f"/orders/export?scroll_id={scroll_id}").status_code == 404

    def test_made_up_scroll_ids_are_refused(self, client: TestClient) -> None:
        assert client.get("/orders/export?scroll_id=WyJhIiw1XQ").status_code == 400
        assert client.get("/orders/export?batch_size=51").status_code == 422
        assert client.get("/orders/export?status=lost").status_code == 422
