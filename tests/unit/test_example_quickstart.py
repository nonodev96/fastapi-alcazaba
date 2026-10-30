"""The apps of the documentation's Quick start, step by step: what each step tells you to check."""

from collections.abc import Iterator
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from examples.quickstart import step1_cache, step2_limits, step3_authz
from fastapi_alcazaba import FastAPICache, FastAPIRateLimiter

OLIVE_OIL = {"name": "Olive oil", "price": 9.5}


def client_of(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(module, "PRODUCTS", {1: module.Product(**OLIVE_OIL)})
    with TestClient(module.app) as client:
        yield client
    FastAPICache.reset()
    for route in module.app.routes:  # the limiters remember refused clients
        for dependency in getattr(route, "dependencies", []):
            if isinstance(dependency.dependency, FastAPIRateLimiter):
                dependency.dependency.forget()


@pytest.fixture(name="step1")
def fixture_step1(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from client_of(step1_cache, monkeypatch)


@pytest.fixture(name="step2")
def fixture_step2(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from client_of(step2_limits, monkeypatch)


@pytest.fixture(name="step3")
def fixture_step3(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from client_of(step3_authz, monkeypatch)


def test_step1_a_miss_then_hits(step1: TestClient) -> None:
    first = step1.get("/products/1")
    assert first.json() == OLIVE_OIL
    assert first.headers["cache-status"] == "Alcazaba; fwd=miss; stored"
    again = step1.get("/products/1")
    assert again.json() == OLIVE_OIL
    assert again.headers["cache-status"].startswith("Alcazaba; hit; ttl=")
    missing = step1.get("/products/2")
    assert (missing.status_code, missing.json()) == (404, {"detail": "No such product"})


def test_step2_five_requests_a_minute(step2: TestClient) -> None:
    for left in range(4, -1, -1):
        response = step2.get("/products/1")
        assert response.status_code == 200
        assert f"r={left};" in response.headers["ratelimit"]
    refused = step2.get("/products/1")
    assert (refused.status_code, refused.json()) == (429, {"detail": "Too Many Requests"})
    assert int(refused.headers["retry-after"]) > 0


class TestStep3:
    def test_only_staff_update(self, step3: TestClient) -> None:
        cheaper = {"name": "Olive oil", "price": 8.0}
        missing = step3.put("/products/1", json=cheaper)
        assert (missing.status_code, missing.headers["www-authenticate"]) == (401, "Bearer")
        ben = {"Authorization": "Bearer ben-token"}
        refused = step3.put("/products/1", json=cheaper, headers=ben)
        assert (refused.status_code, refused.json()) == (403, {"detail": "Forbidden"})
        ana = {"Authorization": "Bearer ana-token"}
        assert step3.put("/products/1", json=cheaper, headers=ana).json() == cheaper

    def test_an_update_drops_the_cached_product(self, step3: TestClient) -> None:
        assert step3.get("/products/1").json() == OLIVE_OIL
        assert step3.get("/products/1").headers["cache-status"].startswith("Alcazaba; hit")
        cheaper = {"name": "Olive oil", "price": 8.0}
        step3.put("/products/1", json=cheaper, headers={"Authorization": "Bearer ana-token"})
        fresh = step3.get("/products/1")
        assert fresh.json() == cheaper
        assert fresh.headers["cache-status"] == "Alcazaba; fwd=miss; stored"

    def test_a_wrong_token(self, step3: TestClient) -> None:
        refused = step3.put("/products/1", json=OLIVE_OIL, headers={"Authorization": "Bearer x"})
        assert (refused.status_code, refused.json()) == (401, {"detail": "Invalid token"})
