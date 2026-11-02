"""The Open Policy Agent example without OPA: it starts, and refuses everything with a 503. Its
policy, against a real OPA, is in tests/integration/test_opa_live.py."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.authz import opa_service
from fastapi_alcazaba.authz.opa import OPAEnforcer

NOWHERE = "http://127.0.0.1:9"  # nothing listens there


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(opa_service, "db", opa_service.Database())
    monkeypatch.setattr(opa_service.enforcer, "query_url", f"{NOWHERE}/v1/data/shop/allow")
    monkeypatch.setattr(opa_service.enforcer, "client", OPAEnforcer(NOWHERE).client)
    with TestClient(opa_service.app) as client:
        yield client


def test_without_opa_nothing_is_let_through(client: TestClient) -> None:
    refused = client.get("/products")
    assert (refused.status_code, refused.json()) == (503, {"detail": "Authorization unavailable"})
    headers = {"Authorization": "Bearer max-token"}
    assert client.post("/orders/2/refund", headers=headers).status_code == 503
    assert not opa_service.db.orders[2].refunded


def test_the_authentication_comes_first(client: TestClient) -> None:
    assert client.get("/orders/1").status_code == 401
    assert client.get("/orders/1", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_the_settings() -> None:
    assert opa_service.Settings(opa_url="http://opa:8181").opa_token is None
