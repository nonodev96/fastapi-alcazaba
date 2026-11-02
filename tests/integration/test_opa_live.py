"""OPAEnforcer and the shop example against a real Open Policy Agent, enabled by its variable:

    OPA_URL=http://localhost:8181

    docker run -d -p 8181:8181 openpolicyagent/opa:1.21.4 run --server --addr 0.0.0.0:8181

The tests load the example's policy (examples/authz/shop.rego) through OPA's API, under an id of
their own, and remove it afterwards.
"""

import logging
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.authz import opa_service
from fastapi_alcazaba.authz.opa import OPAEnforcer

POLICY = Path(opa_service.__file__).parent / "shop.rego"
POLICY_ID = "alcazaba-tests-shop"


@pytest.fixture(name="opa_url", scope="module")
def fixture_opa_url() -> Iterator[str]:
    url = os.environ.get("OPA_URL")
    if url is None:
        pytest.skip("OPA_URL not set")
    with httpx.Client(base_url=url) as client:
        client.put(f"/v1/policies/{POLICY_ID}", content=POLICY.read_bytes()).raise_for_status()
        yield url
        client.delete(f"/v1/policies/{POLICY_ID}").raise_for_status()


@pytest.fixture(name="enforcer")
async def fixture_enforcer(opa_url: str) -> AsyncIterator[OPAEnforcer]:
    enforcer = OPAEnforcer(opa_url, rule="shop.allow")
    yield enforcer
    await enforcer.aclose()


class TestEnforcer:
    async def test_the_policy_decides(self, enforcer: OPAEnforcer) -> None:
        sam = {"name": "sam", "roles": ["support"]}
        assert await enforcer.enforce("*anonymous", "/products", "GET")
        assert not await enforcer.enforce("*anonymous", "/products", "POST")
        assert await enforcer.enforce(sam, {"customer": "ana", "total": 40}, "refund")
        assert not await enforcer.enforce(sam, {"customer": "ana", "total": 250}, "refund")

    async def test_an_undefined_rule_refuses(
        self,
        opa_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        enforcer = OPAEnforcer(opa_url, rule="shop.nothing")
        try:
            with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.authz.opa"):
                assert not await enforcer.enforce("ana", "/products", "GET")
        finally:
            await enforcer.aclose()
        assert "shop.nothing is undefined" in caplog.text

    async def test_a_package_isnt_a_decision(self, opa_url: str) -> None:
        enforcer = OPAEnforcer(opa_url, rule="shop")
        try:
            with pytest.raises(RuntimeError, match="shop gives dict, not a boolean"):
                await enforcer.enforce("ana", "/products", "GET")
        finally:
            await enforcer.aclose()


@pytest.fixture(name="client")
def fixture_client(opa_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(opa_service, "db", opa_service.Database())
    monkeypatch.setattr(opa_service.enforcer, "query_url", f"{opa_url}/v1/data/shop/allow")
    # A client for each test, which the lifespan closes: a circuit breaker of its own too.
    monkeypatch.setattr(opa_service.enforcer, "client", OPAEnforcer(opa_url).client)
    with TestClient(opa_service.app) as client:
        yield client


def token(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}-token"}


class TestExample:
    def test_everybody_reads_the_catalogue(self, client: TestClient) -> None:
        assert client.get("/products").json() == [{"id": 1, "name": "Olive oil", "price": 9.5}]

    def test_customers_read_their_own_orders(self, client: TestClient) -> None:
        assert client.get("/orders/1", headers=token("ana")).json()["customer"] == "ana"
        assert client.get("/orders/3", headers=token("ana")).status_code == 404
        assert client.get("/orders/3", headers=token("sam")).status_code == 200
        assert client.get("/orders/1").status_code == 401

    def test_refunds_by_amount(self, client: TestClient) -> None:
        refused = client.post("/orders/1/refund", headers=token("ana"))
        assert refused.status_code == 403
        assert client.post("/orders/1/refund", headers=token("sam")).json()["refunded"] is True
        refused = client.post("/orders/2/refund", headers=token("sam"))
        assert refused.json()["detail"].endswith("and a manager above 100 euros")
        assert client.post("/orders/2/refund", headers=token("max")).json()["refunded"] is True

    def test_a_service_that_is_down_refuses(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(opa_service.enforcer, "query_url", "http://127.0.0.1:9/v1/data/shop")
        refused = client.get("/products")
        assert refused.status_code == 503
        assert refused.json() == {"detail": "Authorization unavailable"}
