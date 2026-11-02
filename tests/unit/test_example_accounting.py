"""The accounting example: each tenant held to its plan, and every charge recorded."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.accounting.main import app
from fastapi_alcazaba.accounting import meter as meter_module

NOW = datetime(2026, 11, 14, 10, 30, tzinfo=UTC)
TEXT = {"text": "Olive oil is pressed from olives, the fruit of the olive tree, and keeps well."}


class Frozen(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> "Frozen":
        return cls.fromtimestamp(NOW.timestamp(), tz)


@pytest.fixture(name="client")
def fixture_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(meter_module, "datetime", Frozen)
    with TestClient(app) as client:  # the lifespan: a backend of its own for each test
        yield client


def key(tenant: str) -> dict[str, str]:
    return {"X-API-Key": f"{tenant}-key"}


def test_the_free_plan_searches_three_times_a_day(client: TestClient) -> None:
    statuses = [client.get("/search?q=oli", headers=key("acme")).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    refused = client.get("/search?q=oli", headers=key("acme"))
    assert refused.headers["Retry-After"] == str(13 * 3600 + 30 * 60)  # at midnight, UTC
    assert refused.headers["RateLimit-Policy"] == '"searches";q=3;w=86400'


def test_the_pro_plan_has_no_limit(client: TestClient) -> None:
    for _ in range(5):
        response = client.get("/search?q=oli", headers=key("globex"))
        assert response.json() == ["Olive oil", "Olives"]
        assert "RateLimit" not in response.headers


def test_summaries_take_their_tokens(client: TestClient) -> None:
    first = client.post("/summaries", json=TEXT, headers=key("acme"))
    assert first.json()["tokens"] == 27  # 15 words read, 12 written
    assert client.post("/summaries", json=TEXT, headers=key("acme")).status_code == 200
    refused = client.post("/summaries", json={"text": "Olives."}, headers=key("acme"))
    assert (refused.status_code, refused.json()) == (402, {"detail": "Quota exceeded"})


def test_usage(client: TestClient) -> None:
    client.get("/search?q=oli", headers=key("acme"))
    client.post("/summaries", json=TEXT, headers=key("acme"))
    assert client.get("/usage", headers=key("acme")).json() == {
        "searches": {
            "used": 1,
            "limit": 3,
            "remaining": 2,
            "period": "2026-11-14",
            "resets": "2026-11-15T00:00:00+00:00",
        },
        "tokens": {
            "used": 27,
            "limit": 50,
            "remaining": 23,
            "period": "2026-11",
            "resets": "2026-12-01T00:00:00+00:00",
        },
    }
    assert client.get("/usage").status_code == 401


def test_every_charge_is_recorded(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="fastapi_alcazaba.accounting.usage"):
        client.get("/search?q=oli", headers=key("acme"))
        client.post("/summaries", json=TEXT, headers=key("globex"))
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "fastapi_alcazaba.accounting.usage"
    ]
    assert [(r["subject"], r["meter"], r["amount"], r["route"]) for r in records] == [
        ("acme", "searches", 1, "/search"),
        ("globex", "tokens", 27, "/summaries"),
    ]


def test_without_a_key_or_with_a_wrong_one(client: TestClient) -> None:
    missing = client.get("/search?q=oli")
    assert (missing.status_code, missing.headers["WWW-Authenticate"]) == (401, "ApiKey")
    assert client.get("/search?q=oli", headers={"X-API-Key": "nope"}).status_code == 401
