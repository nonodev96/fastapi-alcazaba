"""The response cache example: shared entries, personal ones, tags and get_or_set."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.cache import main
from fastapi_alcazaba import FastAPICache


@pytest.fixture(name="shop")
def fixture_shop(monkeypatch: pytest.MonkeyPatch) -> main.Shop:
    fresh = main.Shop()
    monkeypatch.setattr(main, "shop", fresh)
    return fresh


@pytest.fixture(name="client")
# pylint: disable-next=unused-argument  # requested for its effect: a fresh shop
def fixture_client(shop: main.Shop) -> Iterator[TestClient]:
    with TestClient(main.app) as client:
        yield client
    FastAPICache.reset()


def bearer(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}"}


class TestPublicEntries:
    def test_one_entry_for_everybody(self, client: TestClient, shop: main.Shop) -> None:
        first = client.get("/products/1")
        second = client.get("/products/1", headers=bearer("alice"))
        assert (first.headers["x-fastapi-cache"], second.headers["x-fastapi-cache"]) == (
            "MISS",
            "HIT",
        )
        assert second.headers["cache-control"] == "max-age=600"
        assert second.json() == {"id": 1, "name": "Olive oil", "price": 9.5}
        assert shop.queries == 1

    def test_an_update_drops_its_entry(self, client: TestClient) -> None:
        client.get("/products/1")
        update = {"id": 1, "name": "Olive oil", "price": 10.0}
        assert client.put("/products/1", json=update).status_code == 200
        refreshed = client.get("/products/1")
        assert refreshed.headers["x-fastapi-cache"] == "MISS"
        assert refreshed.json()["price"] == 10.0

    def test_errors_arent_cached(self, client: TestClient, shop: main.Shop) -> None:
        assert client.get("/products/9").status_code == 404
        assert client.get("/products/9").status_code == 404
        assert shop.queries == 2


class TestPersonalEntries:
    def test_each_user_gets_their_own(self, client: TestClient) -> None:
        alice = client.get("/me", headers=bearer("alice"))
        again = client.get("/me", headers=bearer("alice"))
        bob = client.get("/me", headers=bearer("bob"))
        assert [r.headers["x-fastapi-cache"] for r in (alice, again, bob)] == [
            "MISS",
            "HIT",
            "MISS",
        ]
        assert bob.json() == {"user": "bob"}
        assert bob.headers["cache-control"] == "private, max-age=30"
        assert bob.headers["vary"] == "Authorization, Cookie"

    def test_anonymous_requests_are_refused(self, client: TestClient) -> None:
        assert client.get("/me").status_code == 401

    def test_get_or_set(self, client: TestClient, shop: main.Shop) -> None:
        for _ in range(3):
            found = client.get("/me/recommendations", headers=bearer("alice"))
            assert [product["id"] for product in found.json()] == [1, 2]
        assert shop.queries == 1


class TestCachedFunctions:
    def test_prices_in_other_currencies(self, client: TestClient) -> None:
        assert client.get("/products/1/price?currency=USD").json() == {"price": 10.26}
        assert client.get("/products/2/price?currency=USD").json() == {"price": 3.46}
        assert client.get("/products/9/price").status_code == 404
