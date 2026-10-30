"""The limit and offset example: a price list in slices, with the total and the filters kept."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.pagination import limit_offset
from fastapi_alcazaba import FastAPICache


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(limit_offset.app) as client:
        yield client
    FastAPICache.reset()


def skus(body: dict[str, Any]) -> list[str]:
    return [product["sku"] for product in body["items"]]


class TestLimitOffsetExample:
    def test_any_slice_with_the_total(self, client: TestClient) -> None:
        body = client.get("/prices?limit=3&offset=10").json()
        assert skus(body) == ["SKU-011", "SKU-012", "SKU-013"]
        assert (body["total"], body["limit"], body["offset"]) == (60, 3, 10)

    def test_filters_stay_in_the_links(self, client: TestClient) -> None:
        response = client.get("/prices?category=oil&limit=10&offset=10")
        assert response.json()["total"] == 30
        links = response.links
        assert links["next"]["url"] == "/prices?category=oil&limit=10&offset=20"
        assert links["last"]["url"] == "/prices?category=oil&limit=10&offset=20"
        assert links["prev"]["url"] == "/prices?category=oil&limit=10&offset=0"

    def test_past_the_end(self, client: TestClient) -> None:
        body = client.get("/prices?offset=100").json()
        assert (body["items"], body["total"]) == ([], 60)

    def test_the_limit(self, client: TestClient) -> None:
        assert client.get("/prices?limit=51").status_code == 422
        assert client.get("/prices?offset=-1").status_code == 422
