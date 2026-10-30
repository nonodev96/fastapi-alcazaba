"""The pagination example: a catalogue cached page by page, or whole."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.pagination.main import Catalog, app
from fastapi_alcazaba import FastAPICache
from tests.cache_status import served


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client
    FastAPICache.reset()


def queries() -> int:
    catalog: Catalog = app.state.catalog
    return catalog.queries


def ids(body: dict[str, list[dict[str, int]]]) -> list[int]:
    return [item["id"] for item in body["items"]]


class TestOneEntryPerPage:
    def test_pages(self, client: TestClient) -> None:
        assert ids(client.get("/products?page_size=5").json()) == [1, 2, 3, 4, 5]
        assert ids(client.get("/products?page=2&page_size=5").json()) == [6, 7, 8, 9, 10]
        hit = client.get("/products?page=2&page_size=5")
        assert (served(hit.headers), ids(hit.json())) == ("hit", [6, 7, 8, 9, 10])
        assert hit.headers["cache-control"] == "max-age=300"  # public: no Vary, not private
        assert queries() == 2

    def test_the_page_as_clients_see_it(self, client: TestClient) -> None:
        body = client.get("/products?page=12&page_size=5").json()
        assert {key: value for key, value in body.items() if key != "items"} == {
            "total": 60,
            "page": 12,
            "page_size": 5,
            "pages": 12,
        }
        assert ids(body) == [56, 57, 58, 59, 60]

    def test_pages_are_at_most_100_items(self, client: TestClient) -> None:
        assert client.get("/products?page_size=1000").status_code == 422
        assert queries() == 0

    def test_categories(self, client: TestClient) -> None:
        response = client.get("/products?category=oil&page_size=3")
        oil = response.json()
        assert (ids(oil), oil["total"]) == ([2, 5, 8], 20)
        assert response.links["next"]["url"] == "/products?category=oil&page=2&page_size=3"
        assert ids(client.get("/products?page_size=3").json()) == [1, 2, 3]
        assert queries() == 2

    def test_unknown_categories_are_rejected(self, client: TestClient) -> None:
        assert client.get("/products?category=anything").status_code == 422
        assert queries() == 0


class TestOneEntryForTheWholeList:
    def test_every_page_comes_from_one_query(self, client: TestClient) -> None:
        url = "/categories/oil/products?page={}&page_size=8"
        pages = [client.get(url.format(n)).json() for n in (1, 2, 3)]
        assert [ids(page) for page in pages] == [
            [2, 5, 8, 11, 14, 17, 20, 23],
            [26, 29, 32, 35, 38, 41, 44, 47],
            [50, 53, 56, 59],
        ]
        assert ids(client.get("/categories/oil/products?page_size=2").json()) == [2, 5]
        assert queries() == 1

    def test_each_category_has_its_entry(self, client: TestClient) -> None:
        assert ids(client.get("/categories/fruit/products?page_size=2").json()) == [3, 6]
        assert ids(client.get("/categories/vegetables/products?page_size=2").json()) == [1, 4]
        assert queries() == 2


class TestByCursor:
    def test_newest_first(self, client: TestClient) -> None:
        first = client.get("/products/newest?limit=3").json()
        assert ids(first) == [60, 59, 58]
        second = client.get(f"/products/newest?limit=3&cursor={first['next_cursor']}").json()
        assert ids(second) == [57, 56, 55]
        assert queries() == 2

    def test_products_added_meanwhile_dont_shift_the_pages(self, client: TestClient) -> None:
        first = client.get("/products/newest?limit=3").json()
        client.post("/products", json={"name": "Picual", "category": "oil", "price": 4.5})
        second = client.get(f"/products/newest?limit=3&cursor={first['next_cursor']}").json()
        assert ids(second) == [57, 56, 55]
        assert ids(client.get("/products/newest?limit=3").json()) == [61, 60, 59]

    def test_made_up_cursors_are_refused(self, client: TestClient) -> None:
        assert client.get("/products/newest?cursor=MTA").status_code == 400


class TestWrites:
    def test_adding_a_product_drops_every_page(self, client: TestClient) -> None:
        empty = client.get("/products?page=2&page_size=20&category=oil")  # the 20 fit in page 1
        assert ids(empty.json()) == []
        client.get("/categories/oil/products")
        added = client.post("/products", json={"name": "Picual", "category": "oil", "price": 4.5})
        assert (added.status_code, added.json()["id"]) == (201, 61)
        page = client.get("/products?page=2&page_size=20&category=oil")
        assert (served(page.headers), page.json()["total"]) == ("miss", 21)
        assert ids(page.json()) == [61]
        assert client.get("/categories/oil/products").json()["total"] == 21
        assert queries() == 4
