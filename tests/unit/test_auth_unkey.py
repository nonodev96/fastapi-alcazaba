"""UnkeyStrategy, UnkeyClient and UnkeyKeys, against a fake of Unkey's API (v2) that answers as
its OpenAPI says."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba.auth import FastAPIAuth
from fastapi_alcazaba.auth.strategies import UnkeyStrategy
from fastapi_alcazaba.auth.strategies.unkey import (
    CreatedKey,
    CreditRefill,
    KeyRatelimit,
    UnkeyClient,
    UnkeyError,
    UnkeyKey,
    UnkeyKeys,
    UnkeyUnavailable,
)
from fastapi_alcazaba.telemetry import Metrics
from tests.unit.test_metrics import points

ROOT_KEY = "unkey_root"  # noqa: S105  # the fake's
#: The keys the fake knows: what verifying each one answers.
KEYS: dict[str, dict[str, Any]] = {
    "sk_ana": {
        "valid": True,
        "code": "VALID",
        "keyId": "key_ana",
        "name": "Ana's laptop",
        "identity": {"id": "id_1", "externalId": "user_ana"},
        "meta": {"plan": "pro"},
        "credits": 99,
        "expires": 1_830_000_000_000,
        "permissions": ["documents.read", "documents.write"],
        "roles": ["editor"],
        "ratelimits": [
            {
                "id": "rl_1",
                "name": "requests",
                "limit": 100,
                "duration": 60_000,
                "reset": 42_000,
                "remaining": 97,
                "exceeded": False,
                "autoApply": True,
            }
        ],
    },
    "sk_gone": {"valid": False, "code": "NOT_FOUND"},
    "sk_off": {"valid": False, "code": "DISABLED", "keyId": "key_off"},
    "sk_old": {"valid": False, "code": "EXPIRED", "keyId": "key_old"},
    "sk_reader": {"valid": False, "code": "INSUFFICIENT_PERMISSIONS", "keyId": "key_r"},
    "sk_busy": {
        "valid": False,
        "code": "RATE_LIMITED",
        "keyId": "key_busy",
        "ratelimits": [
            {
                "id": "rl_2",
                "name": "requests",
                "limit": 10,
                "duration": 60_000,
                "reset": 12_300,
                "remaining": 0,
                "exceeded": True,
                "autoApply": True,
            }
        ],
    },
    "sk_spent": {"valid": False, "code": "USAGE_EXCEEDED", "keyId": "key_spent", "credits": 0},
    "sk_future": {"valid": False, "code": "QUARANTINED", "keyId": "key_q"},
}

Answer = Callable[[httpx.Request], httpx.Response]


def unkey(request: httpx.Request) -> httpx.Response:
    """Stands in for Unkey's API."""
    meta = {"requestId": "req_123"}
    if request.headers.get("Authorization") != f"Bearer {ROOT_KEY}":
        error = {"title": "Unauthorized", "detail": "invalid root key", "status": 401, "type": "x"}
        return httpx.Response(401, json={"meta": meta, "error": error})
    body = json.loads(request.content)
    if request.url.path == "/v2/keys.verifyKey":
        return httpx.Response(200, json={"meta": meta, "data": KEYS[body["key"]]})
    if request.url.path == "/v2/keys.createKey":
        created = {"key": "sk_new", "keyId": "key_new"}
        return httpx.Response(200, json={"meta": meta, "data": created})
    if request.url.path == "/v2/keys.deleteKey":
        return httpx.Response(200, json={"meta": meta, "data": {}})
    return httpx.Response(404, json={"meta": meta, "error": {"detail": "no such operation"}})


class FakeUnkey:
    """An UnkeyClient whose calls reach `answer`, recorded."""

    def __init__(self, answer: Answer = unkey, root_key: str = ROOT_KEY) -> None:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return answer(request)

        transport = httpx.MockTransport(record)
        self.client = UnkeyClient(root_key, client=httpx.AsyncClient(transport=transport))

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def answering(*args: Any, **kwargs: Any) -> Answer:
    return lambda _: httpx.Response(*args, **kwargs)


def app_with(strategy: UnkeyStrategy) -> TestClient:
    auth = FastAPIAuth(strategy)
    app = FastAPI()

    @app.get("/me")
    async def me(user: Annotated[Any, Depends(auth.authenticate())]) -> Any:
        return user

    return TestClient(app, raise_server_exceptions=False)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestVerification:
    def test_a_valid_key(self) -> None:
        fake = FakeUnkey()
        users: list[UnkeyKey] = []

        def load_user(key: UnkeyKey) -> dict[str, Any]:
            users.append(key)
            return {"id": key.external_id, "plan": key.meta["plan"]}

        client = app_with(UnkeyStrategy(fake.client, load_user))
        user = client.get("/me", headers=bearer("sk_ana")).json()
        assert user == {"id": "user_ana", "plan": "pro"}
        assert len(users) == 1
        key = users[0]
        assert (key.key_id, key.name, key.credits, key.roles) == (
            "key_ana",
            "Ana's laptop",
            99,
            ("editor",),
        )
        assert key.expires == datetime.fromtimestamp(1_830_000_000, UTC)
        assert key.ratelimits[0].reset == 42.0
        assert key.request_id == "req_123"

    def test_without_verify_the_key_is_the_user(self) -> None:
        user = app_with(UnkeyStrategy(FakeUnkey().client)).get("/me", headers=bearer("sk_ana"))
        assert user.json()["key_id"] == "key_ana"

    def test_the_query(self) -> None:
        fake = FakeUnkey()
        strategy = UnkeyStrategy(
            fake.client,
            permissions="documents.read AND users.view",
            cost=3,
            ratelimits=["tokens"],
            keyspaces=["ks_1"],
            tags=["route=/me"],
        )
        app_with(strategy).get("/me", headers=bearer("sk_ana"))
        assert len(fake.requests) == 1
        request = fake.requests[0]
        assert str(request.url) == "https://api.unkey.com/v2/keys.verifyKey"
        assert request.headers["Authorization"] == f"Bearer {ROOT_KEY}"
        assert fake.bodies() == [
            {
                "key": "sk_ana",
                "credits": {"cost": 3},
                "permissions": "documents.read AND users.view",
                "ratelimits": [{"name": "tokens"}],
                "keyspaces": ["ks_1"],
                "tags": ["route=/me"],
            }
        ]

    @pytest.mark.parametrize("key", ["sk_gone", "sk_off", "sk_old", "sk_future"])
    def test_keys_that_arent_valid_fail(self, key: str) -> None:
        refused = app_with(UnkeyStrategy(FakeUnkey().client)).get("/me", headers=bearer(key))
        assert (refused.status_code, refused.headers["WWW-Authenticate"]) == (401, "Bearer")

    def test_verify_may_refuse(self) -> None:
        client = app_with(UnkeyStrategy(FakeUnkey().client, lambda _: None))
        assert client.get("/me", headers=bearer("sk_ana")).status_code == 401

    def test_without_a_key_unkey_isnt_asked(self) -> None:
        fake = FakeUnkey()
        client = app_with(UnkeyStrategy(fake.client))
        assert client.get("/me").status_code == 401
        assert client.get("/me", headers={"Authorization": "Basic YTpi"}).status_code == 401
        assert fake.requests == []

    @pytest.mark.parametrize("key", ["", "sk ana", "sk_ñ", "k" * 513, '{"$ne": 1}'])
    def test_a_malformed_key_isnt_sent(self, key: str) -> None:
        fake = FakeUnkey()
        response = app_with(UnkeyStrategy(fake.client, header="X-API-Key")).get(
            "/me", headers={b"X-API-Key": key.encode()}
        )
        assert response.status_code == 401
        assert fake.requests == []

    def test_a_header_of_its_own(self) -> None:
        fake = FakeUnkey()
        client = app_with(UnkeyStrategy(fake.client, header="X-API-Key"))
        assert client.get("/me", headers={"X-API-Key": "sk_ana"}).status_code == 200
        refused = client.get("/me", headers=bearer("sk_ana"))  # not where it's read
        assert refused.status_code == 401
        assert "WWW-Authenticate" not in refused.headers


class TestRefusals:
    def test_insufficient_permissions_a_403(self) -> None:
        client = app_with(UnkeyStrategy(FakeUnkey().client))
        refused = client.get("/me", headers=bearer("sk_reader"))
        assert refused.status_code == 403
        assert refused.json() == {"detail": "Insufficient permissions"}

    def test_rate_limited_a_429_with_retry_after(self) -> None:
        refused = app_with(UnkeyStrategy(FakeUnkey().client)).get("/me", headers=bearer("sk_busy"))
        assert (refused.status_code, refused.headers["Retry-After"]) == (429, "13")

    @pytest.mark.parametrize(("status", "expected"), [(429, 429), (402, 402)])
    def test_no_credits_left(self, status: int, expected: int) -> None:
        strategy = UnkeyStrategy(FakeUnkey().client, credits_status=status)
        refused = app_with(strategy).get("/me", headers=bearer("sk_spent"))
        assert (refused.status_code, refused.json()) == (expected, {"detail": "No credits left"})

    @pytest.mark.parametrize(
        "answer",
        [
            answering(503, text="Service Unavailable"),
            answering(429, json={"error": {"detail": "workspace rate limit"}}),
            answering(500, json={"error": {"detail": "boom"}}),
        ],
    )
    def test_unkey_unavailable_a_503(
        self,
        answer: Answer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.auth.strategies.unkey"):
            refused = app_with(UnkeyStrategy(FakeUnkey(answer).client)).get(
                "/me", headers=bearer("sk_ana")
            )
        assert (refused.status_code, refused.json()) == (
            503,
            {"detail": "Authentication unavailable"},
        )
        assert "Unkey is unavailable" in caplog.text
        assert "sk_ana" not in caplog.text  # the key is never logged

    def test_no_answer_a_503(self) -> None:
        def fail(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = app_with(UnkeyStrategy(FakeUnkey(fail).client))
        refused = client.get("/me", headers=bearer("sk_ana"))
        assert refused.status_code == 503

    def test_a_root_key_unkey_refuses_is_a_500(self) -> None:
        client = app_with(UnkeyStrategy(FakeUnkey(root_key="wrong").client))
        assert client.get("/me", headers=bearer("sk_ana")).status_code == 500

    def test_websockets(self) -> None:
        auth = FastAPIAuth(UnkeyStrategy(FakeUnkey().client))
        app = FastAPI()

        @app.websocket("/ws")
        async def chat(
            websocket: WebSocket,
            user: Annotated[Any, Depends(auth.authenticate())],
        ) -> None:
            await websocket.accept()
            await websocket.send_json(user.key_id)
            await websocket.close()

        client = TestClient(app)
        with client.websocket_connect("/ws", headers=bearer("sk_ana")) as websocket:
            assert websocket.receive_json() == "key_ana"
        for key, code in (("sk_busy", 1008), ("sk_gone", 1008)):
            with (
                pytest.raises(WebSocketDisconnect) as refused,
                client.websocket_connect("/ws", headers=bearer(key)),
            ):
                pass
            assert refused.value.code == code

    def test_websockets_while_unkey_is_down(self) -> None:
        auth = FastAPIAuth(UnkeyStrategy(FakeUnkey(answering(503)).client))
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(auth.authenticate())])
        async def chat(websocket: WebSocket) -> None:
            await websocket.accept()

        with (
            pytest.raises(WebSocketDisconnect) as refused,
            TestClient(app).websocket_connect("/ws", headers=bearer("sk_ana")),
        ):
            pass
        assert refused.value.code == 1013


def test_verifications_are_counted() -> None:
    reader = InMemoryMetricReader()
    metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
    client = app_with(UnkeyStrategy(FakeUnkey().client, metrics=metrics))
    for key in ("sk_ana", "sk_ana", "sk_gone", "sk_busy"):
        client.get("/me", headers=bearer(key))
    codes = {
        attributes["fastapi_alcazaba.code"]: value
        for attributes, value in points(reader, "fastapi_alcazaba.auth.unkey")
    }
    assert codes == {"VALID": 2, "NOT_FOUND": 1, "RATE_LIMITED": 1}


class TestUnkeyClient:
    def test_configuration(self) -> None:
        with pytest.raises(ValueError, match="root key is empty"):
            UnkeyClient("")
        with pytest.raises(ValueError, match="https://"):
            UnkeyClient(ROOT_KEY, url="http://unkey.example.com")
        with pytest.raises(ValueError, match="no query"):
            UnkeyClient(ROOT_KEY, url="https://unkey.example.com/?a=1")
        with pytest.raises(ValueError, match="timeout"):
            UnkeyClient(ROOT_KEY, timeout=0)
        local = UnkeyClient(SecretStr(ROOT_KEY), url="http://localhost:3000/")
        assert local.url == "http://localhost:3000"

    async def test_errors(self) -> None:
        refused = FakeUnkey(root_key="wrong").client
        with pytest.raises(UnkeyError, match="401: invalid root key") as error:
            await refused.call("keys.verifyKey", {"key": "sk_ana"})
        assert (error.value.status, error.value.request_id) == (401, "req_123")
        something_else = FakeUnkey(answering(200, text="<html>a proxy</html>")).client
        with pytest.raises(UnkeyError, match="didn't answer as Unkey does"):
            await something_else.call("keys.verifyKey", {"key": "sk_ana"})
        with pytest.raises(UnkeyUnavailable, match="502: Bad Gateway"):
            await FakeUnkey(answering(502)).client.call("keys.verifyKey", {"key": "k"})

    async def test_its_own_client_has_a_circuit_breaker(self) -> None:
        client = UnkeyClient(ROOT_KEY, url="http://127.0.0.1:9", timeout=0.5)
        try:
            for _ in range(6):
                with pytest.raises(UnkeyUnavailable) as unavailable:
                    await client.call("keys.verifyKey", {"key": "k"})
            assert "CircuitOpenError" in str(unavailable.value)
        finally:
            await client.aclose()
        assert client.client.is_closed

    async def test_a_client_it_was_given_stays_open(self) -> None:
        fake = FakeUnkey()
        await fake.client.aclose()
        assert not fake.client.client.is_closed


class TestStrategyConfiguration:
    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"permissions": ""}, "permissions is a query"),
            ({"cost": -1}, "cost can't be negative"),
            ({"keyspaces": ["a"] * 6}, "at most 5 keyspaces"),
            ({"credits_status": 403}, "credits_status"),
        ],
    )
    def test_invalid(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            UnkeyStrategy(FakeUnkey().client, **options)

    def test_lists_not_strings(self) -> None:
        with pytest.raises(TypeError, match="lists of names"):
            UnkeyStrategy(FakeUnkey().client, tags="env=prod")

    def test_in_the_openapi_schema(self) -> None:
        schemes = (
            app_with(UnkeyStrategy(FakeUnkey().client, header="X-API-Key"))
            .get("/openapi.json")
            .json()["components"]["securitySchemes"]
        )
        assert schemes == {"unkey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}


class TestKeys:
    async def test_create(self) -> None:
        fake = FakeUnkey()
        keys = UnkeyKeys(fake.client, api_id="api_123", prefix="sk_live")
        created = await keys.create(
            external_id="user_ana",
            name="Ana's laptop",
            expires=datetime(2027, 1, 1, tzinfo=UTC),
            credits_left=1000,
            refill=CreditRefill(1000, day=1),
            ratelimits=[KeyRatelimit("requests", 100, timedelta(minutes=1))],
            permissions=["documents.read"],
            roles=["reader"],
            meta={"plan": "pro"},
        )
        assert created == CreatedKey(key=SecretStr("sk_new"), key_id="key_new")
        assert created.key.get_secret_value() == "sk_new"
        assert "sk_new" not in repr(created)  # a SecretStr: not in logs
        assert fake.bodies() == [
            {
                "apiId": "api_123",
                "prefix": "sk_live",
                "externalId": "user_ana",
                "name": "Ana's laptop",
                "expires": 1798761600000,
                "credits": {
                    "remaining": 1000,
                    "refill": {"interval": "monthly", "amount": 1000, "refillDay": 1},
                },
                "ratelimits": [
                    {"name": "requests", "limit": 100, "duration": 60000, "autoApply": True}
                ],
                "permissions": ["documents.read"],
                "roles": ["reader"],
                "meta": {"plan": "pro"},
            }
        ]

    async def test_a_key_with_nothing_but_its_api(self) -> None:
        fake = FakeUnkey()
        await UnkeyKeys(fake.client, api_id="api_123").create()
        assert fake.bodies() == [{"apiId": "api_123"}]

    async def test_delete(self) -> None:
        fake = FakeUnkey()
        await UnkeyKeys(fake.client, api_id="api_123").delete("key_ana", permanent=True)
        assert fake.bodies() == [{"keyId": "key_ana", "permanent": True}]
        assert str(fake.requests[0].url).endswith("/v2/keys.deleteKey")

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"external_id": "ana@example.com"}, "external_id"),
            ({"expires": datetime(2027, 1, 1)}, "aware datetime"),
            ({"credits_left": -1}, "can't be negative"),
            ({"refill": CreditRefill(10)}, "give the key some"),
        ],
    )
    async def test_invalid(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            await UnkeyKeys(FakeUnkey().client, api_id="api_123").create(**options)

    async def test_unkey_must_give_the_key(self) -> None:
        empty = FakeUnkey(answering(200, json={"meta": {"requestId": "r"}, "data": {}})).client
        with pytest.raises(UnkeyError, match="without a key"):
            await UnkeyKeys(empty, api_id="api_123").create()

    def test_configuration(self) -> None:
        with pytest.raises(ValueError, match="api_id"):
            UnkeyKeys(FakeUnkey().client, api_id="a")
        with pytest.raises(ValueError, match="prefix"):
            UnkeyKeys(FakeUnkey().client, api_id="api_123", prefix="sk-live")
        with pytest.raises(ValueError, match="3 to 128 characters"):
            KeyRatelimit("ab", 1, timedelta(seconds=1))
        with pytest.raises(ValueError, match="a second or more"):
            KeyRatelimit("requests", 1, timedelta(milliseconds=500))
        with pytest.raises(ValueError, match="1 request or more"):
            KeyRatelimit("requests", 0, timedelta(seconds=1))
        with pytest.raises(ValueError, match="daily or monthly"):
            CreditRefill(1, interval="weekly")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="monthly ones"):
            CreditRefill(1, interval="daily", day=3)
        with pytest.raises(ValueError, match="1 credit or more"):
            CreditRefill(0)

    async def test_delete_takes_the_id_not_the_key(self) -> None:
        with pytest.raises(ValueError, match="not the key"):
            await UnkeyKeys(FakeUnkey().client, api_id="api_123").delete("sk-live.key!")
