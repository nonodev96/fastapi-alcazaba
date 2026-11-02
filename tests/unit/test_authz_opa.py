"""OPAEnforcer: Open Policy Agent decides for FastAPIAuthz and AuthzMiddleware, over its REST API,
and a service that doesn't answer refuses with a 503."""

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fastapi_alcazaba.authz import FastAPIAuthz, PolicyUnavailable
from fastapi_alcazaba.authz.opa import OPAEnforcer

ROLES = {"ana": ["editor"], "bob": ["reader"]}
PERMISSIONS = {"editor": {"GET", "PUT", "publish"}, "reader": {"GET"}}

Answer = Callable[[httpx.Request], httpx.Response]


def policy(request: httpx.Request) -> httpx.Response:
    """Stands in for OPA and its policy: the roles of the subject allow actions."""
    query = json.loads(request.content)["input"]
    subject = query["subject"]
    roles = subject["roles"] if isinstance(subject, dict) else ROLES.get(subject, [])
    allowed = any(query["action"] in PERMISSIONS.get(role, ()) for role in roles)
    return httpx.Response(200, json={"result": allowed, "decision_id": "4ca636c1"})


class FakeOPA:
    """An httpx2 client whose requests the `answer` gets, recorded."""

    def __init__(self, answer: Answer = policy) -> None:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return answer(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(record))

    def enforcer(self, **options: Any) -> OPAEnforcer:
        return OPAEnforcer("http://opa:8181", client=self.client, **options)


def answering(*args: Any, **kwargs: Any) -> Answer:
    return lambda _: httpx.Response(*args, **kwargs)


class TestEnforce:
    async def test_the_result_decides(self) -> None:
        opa = FakeOPA().enforcer()
        assert await opa.enforce("ana", "/articles/7", "PUT")
        assert not await opa.enforce("bob", "/articles/7", "PUT")
        assert not await opa.enforce("mallory", "/articles/7", "GET")

    async def test_the_query(self) -> None:
        fake = FakeOPA()
        opa = fake.enforcer(rule="app.rbac.allow", token="s3cret")  # noqa: S106
        user = {"name": "ana", "roles": ["editor"]}
        await opa.enforce(user, {"type": "order", "total": 120}, "refund", {"hour": 10})
        assert len(fake.requests) == 1
        request = fake.requests[0]
        assert (request.method, str(request.url)) == (
            "POST",
            "http://opa:8181/v1/data/app/rbac/allow",
        )
        assert request.headers["Authorization"] == "Bearer s3cret"
        assert json.loads(request.content) == {
            "input": {
                "subject": user,
                "resource": {"type": "order", "total": 120},
                "action": "refund",
                "context": {"hour": 10},
            }
        }

    async def test_without_a_token_no_authorization(self) -> None:
        fake = FakeOPA()
        await fake.enforcer(token=SecretStr("s3cret")).enforce("ana", "/", "GET")
        await fake.enforcer().enforce("ana", "/", "GET")
        assert ["Authorization" in request.headers for request in fake.requests] == [True, False]

    async def test_an_undefined_rule_refuses(self, caplog: pytest.LogCaptureFixture) -> None:
        """No `result`: the rule has no default, or doesn't exist."""
        opa = FakeOPA(answering(200, json={"decision_id": "4ca636c1"})).enforcer()
        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.authz.opa"):
            assert not await opa.enforce("ana", "/articles/7", "GET")
        assert "authz.allow is undefined for this input" in caplog.text

    @pytest.mark.parametrize("result", [1, "true", {"allow": True}, None, []])
    async def test_only_a_boolean_is_a_decision(self, result: Any) -> None:
        opa = FakeOPA(answering(200, json={"result": result})).enforcer()
        with pytest.raises(RuntimeError, match="not a boolean"):
            await opa.enforce("ana", "/articles/7", "GET")

    @pytest.mark.parametrize("body", [b"<html>a proxy</html>", b"[true]"])
    async def test_something_else_answering_is_an_error(self, body: bytes) -> None:
        opa = FakeOPA(answering(200, content=body)).enforcer()
        with pytest.raises(RuntimeError, match="didn't answer as Open Policy Agent does"):
            await opa.enforce("ana", "/articles/7", "GET")

    @pytest.mark.parametrize("status", [400, 401, 404])
    async def test_a_query_it_refuses_is_an_error(self, status: int) -> None:
        error = {"code": "unauthorized", "message": "missing or invalid bearer token"}
        opa = FakeOPA(answering(status, json=error)).enforcer()
        with pytest.raises(RuntimeError, match=f"\\({status}\\): missing or invalid bearer token"):
            await opa.enforce("ana", "/articles/7", "GET")


class TestUnavailable:
    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_a_server_error(self, status: int) -> None:
        error = {"code": "internal_error", "message": "eval_conflict_error"}
        opa = FakeOPA(answering(status, json=error)).enforcer()
        with pytest.raises(PolicyUnavailable, match=f"answered {status}: eval_conflict_error"):
            await opa.enforce("ana", "/articles/7", "GET")

    async def test_a_server_error_that_isnt_json(self) -> None:
        opa = FakeOPA(answering(502, text="Bad Gateway")).enforcer()
        with pytest.raises(PolicyUnavailable, match="answered 502: Bad Gateway"):
            await opa.enforce("ana", "/articles/7", "GET")

    @pytest.mark.parametrize(
        "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("too slow")]
    )
    async def test_no_answer(self, error: Exception) -> None:
        def fail(_: httpx.Request) -> httpx.Response:
            raise error

        opa = FakeOPA(fail).enforcer()
        with pytest.raises(PolicyUnavailable, match="didn't answer"):
            await opa.enforce("ana", "/articles/7", "GET")

    async def test_its_own_client_has_a_circuit_breaker(self) -> None:
        """Nothing listens on port 9: refused at once, then not even tried."""
        opa = OPAEnforcer("http://127.0.0.1:9", timeout=0.5)
        try:
            for _ in range(6):
                with pytest.raises(PolicyUnavailable) as unavailable:
                    await opa.enforce("ana", "/articles/7", "GET")
            assert "CircuitOpenError" in str(unavailable.value)
        finally:
            await opa.aclose()
        assert opa.client.is_closed


class TestConfiguration:
    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("localhost:8181", "http:// or https://"),
            ("ftp://opa", "http:// or https://"),
            ("http://", "http:// or https://"),
            ("http://opa:8181?pretty", "without a query"),
        ],
    )
    def test_the_url(self, url: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            OPAEnforcer(url)

    @pytest.mark.parametrize("rule", ["", "authz/allow", "data.authz..allow", "1st.allow", "a b"])
    def test_the_rule(self, rule: str) -> None:
        with pytest.raises(ValueError, match="rule is a reference"):
            OPAEnforcer("http://opa:8181", rule=rule)

    @pytest.mark.parametrize("token", ["", SecretStr("")])
    def test_an_empty_token(self, token: str | SecretStr) -> None:
        """An empty OPA_TOKEN is a mistake, not the absence of a token."""
        with pytest.raises(ValueError, match="token is empty"):
            OPAEnforcer("http://opa:8181", token=token)

    def test_the_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout"):
            OPAEnforcer("http://opa:8181", timeout=0)

    async def test_a_path_in_front(self) -> None:
        fake = FakeOPA()
        opa = OPAEnforcer("https://gateway/opa/", client=fake.client, rule="shop.allow")
        await opa.enforce("ana", "/", "GET")
        assert str(fake.requests[0].url) == "https://gateway/opa/v1/data/shop/allow"

    async def test_a_client_it_was_given_stays_open(self) -> None:
        fake = FakeOPA()
        await fake.enforcer().aclose()
        assert not fake.client.is_closed


def user_header(x_user: Annotated[str | None, Header()] = None) -> str | None:
    return x_user


def test_in_an_app() -> None:
    authz = FastAPIAuthz(FakeOPA().enforcer(), subject=user_header)
    app = FastAPI()

    @app.put("/articles/{article_id}", dependencies=[Depends(authz)])
    async def update(article_id: int) -> int:
        return article_id

    @app.post("/articles/{article_id}/publish")
    async def publish(
        article_id: int,
        user: Annotated[str, Depends(authz.require("articles", "publish"))],
    ) -> dict[str, Any]:
        return {"article": article_id, "by": user}

    client = TestClient(app)
    assert client.put("/articles/1", headers={"X-User": "ana"}).status_code == 200
    assert client.put("/articles/1", headers={"X-User": "bob"}).status_code == 403
    assert client.post("/articles/1/publish", headers={"X-User": "ana"}).json()["by"] == "ana"
    assert client.post("/articles/1/publish", headers={"X-User": "bob"}).status_code == 403
    assert client.put("/articles/1").status_code == 401


def test_in_an_app_a_service_that_is_down() -> None:
    authz = FastAPIAuthz(FakeOPA(answering(503, text="")).enforcer(), subject=user_header)
    app = FastAPI()

    @app.get("/articles", dependencies=[Depends(authz)])
    async def articles() -> list[str]:
        return ["first"]

    refused = TestClient(app).get("/articles", headers={"X-User": "ana"})
    assert (refused.status_code, refused.json()) == (503, {"detail": "Authorization unavailable"})
