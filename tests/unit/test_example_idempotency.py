"""The idempotency example: payments that retries never charge twice."""

import asyncio
import threading
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from examples.idempotency import main
from examples.idempotency.main import NewPayment, app

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}
PAYMENT = {"amount": 1250, "currency": "EUR", "description": "Order 1042"}


class Provider:
    """Stands in for charge_card: records the charges, and holds them while the test wants."""

    def __init__(self) -> None:
        self.charges: list[tuple[str, int]] = []  # (idempotency key, amount)
        self.finish = threading.Event()
        self.finish.set()
        self.started = threading.Event()

    async def charge(self, _customer: str, payment: NewPayment, idempotency_key: str) -> str:
        self.charges.append((idempotency_key, payment.amount))
        self.started.set()
        while not self.finish.is_set():
            await asyncio.sleep(0.005)
        return f"ch_test_{len(self.charges)}"


@pytest.fixture(name="provider")
def fixture_provider(monkeypatch: pytest.MonkeyPatch) -> Provider:
    fake = Provider()
    monkeypatch.setattr(main, "charge_card", fake.charge)
    return fake


@pytest.fixture(name="client")
# pylint: disable-next=unused-argument  # requested for its effect: the stand-in provider
def fixture_client(provider: Provider) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def new_key() -> str:
    return str(uuid.uuid4())


def pay(
    client: TestClient,
    key: str | None,
    customer: Mapping[str, str] | None = None,
    **changes: Any,
) -> httpx.Response:
    headers = {
        **(ALICE if customer is None else customer),
        **({"Idempotency-Key": key} if key is not None else {}),
    }
    return client.post("/payments", json={**PAYMENT, **changes}, headers=headers)


class TestRetries:
    def test_a_retry_gets_the_first_response(self, client: TestClient, provider: Provider) -> None:
        key = new_key()
        first = pay(client, key)
        assert first.status_code == 201
        assert "idempotent-replayed" not in first.headers
        assert first.json()["customer"] == "alice"
        retry = pay(client, key)
        assert (retry.status_code, retry.json()) == (201, first.json())
        assert retry.headers["idempotent-replayed"] == "true"
        assert provider.charges == [(f"alice:{key}", 1250)]
        assert client.get("/payments", headers=ALICE).json() == [first.json()]

    def test_a_new_key_is_a_new_payment(self, client: TestClient, provider: Provider) -> None:
        first, second = pay(client, new_key()), pay(client, new_key())
        assert first.json()["id"] != second.json()["id"]
        assert len(provider.charges) == 2
        assert len(client.get("/payments", headers=ALICE).json()) == 2

    def test_payments_need_a_key(self, client: TestClient, provider: Provider) -> None:
        response = pay(client, None)
        assert response.status_code == 400
        assert response.json() == {"detail": "This request needs an Idempotency-Key header"}
        assert provider.charges == []

    def test_a_rejected_request_can_be_corrected_with_the_same_key(
        self,
        client: TestClient,
        provider: Provider,
    ) -> None:
        key = new_key()
        assert pay(client, key, amount=-5).status_code == 422  # not kept: nothing ran
        corrected = pay(client, key)
        assert corrected.status_code == 201
        assert "idempotent-replayed" not in corrected.headers
        assert provider.charges == [(f"alice:{key}", 1250)]


class TestConflicts:
    def test_a_retry_while_the_first_runs_gets_409(
        self,
        client: TestClient,
        provider: Provider,
    ) -> None:
        key = new_key()
        provider.finish.clear()
        with ThreadPoolExecutor(1) as pool:
            try:
                first = pool.submit(pay, client, key)
                assert provider.started.wait(5)
                retry = pay(client, key)
            finally:
                provider.finish.set()
            assert first.result(5).status_code == 201
        assert (retry.status_code, retry.headers["retry-after"]) == (409, "1")
        assert retry.json() == {
            "detail": "A request with this Idempotency-Key is still being processed"
        }
        later = pay(client, key)
        assert (later.headers["idempotent-replayed"], later.json()) == (
            "true",
            first.result().json(),
        )
        assert len(provider.charges) == 1

    def test_the_same_key_with_another_body_gets_422(
        self,
        client: TestClient,
        provider: Provider,
    ) -> None:
        key = new_key()
        pay(client, key)
        other = pay(client, key, amount=99_000)
        assert other.status_code == 422
        assert other.json() == {"detail": "This Idempotency-Key was used with a different request"}
        assert provider.charges == [(f"alice:{key}", 1250)]


class TestPerCustomer:
    def test_keys_are_per_customer(self, client: TestClient, provider: Provider) -> None:
        key = new_key()
        alices = pay(client, key)
        bobs = pay(client, key, BOB)
        assert bobs.status_code == 201
        assert "idempotent-replayed" not in bobs.headers
        assert (alices.json()["customer"], bobs.json()["customer"]) == ("alice", "bob")
        assert provider.charges == [(f"alice:{key}", 1250), (f"bob:{key}", 1250)]
        assert [p["id"] for p in client.get("/payments", headers=BOB).json()] == [bobs.json()["id"]]

    def test_a_refreshed_token_still_gets_the_first_response(
        self,
        client: TestClient,
        provider: Provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        key = new_key()
        first = pay(client, key)
        monkeypatch.setitem(main.TOKENS, "alice-refreshed-token", "alice")
        retry = pay(client, key, {"Authorization": "Bearer alice-refreshed-token"})
        assert (retry.headers["idempotent-replayed"], retry.json()) == ("true", first.json())
        assert len(provider.charges) == 1

    def test_customers_need_a_token(self, client: TestClient, provider: Provider) -> None:
        for customer in ({}, {"Authorization": "Bearer made-up"}):
            for key in (new_key(), None):
                response = pay(client, key, customer)
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == "Bearer"
            assert client.get("/payments", headers=customer).status_code == 401
        assert provider.charges == []
