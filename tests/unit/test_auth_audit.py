"""The audit trail of authentication and authorization: FastAPIAuth's failed authentications,
logins and missing scopes; FastAPIAuthz's and AuthzMiddleware's refusals."""

import base64
import uuid
from types import SimpleNamespace
from typing import Annotated, Any

import casbin
import pytest
from fastapi import Depends, FastAPI, Header, Security
from fastapi.testclient import TestClient
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection

from fastapi_alcazaba import FastAPIAudit
from fastapi_alcazaba.audit import AuditEvent, MemoryAuditSink
from fastapi_alcazaba.auth import AuthenticationFailed, FastAPIAuth, Strategy
from fastapi_alcazaba.auth.strategies.base import _actor
from fastapi_alcazaba.authz import AuthzMiddleware, FastAPIAuthz, PolicyUnavailable, subject_id

RBAC = """
[request_definition]
r = sub, obj, act
[policy_definition]
p = sub, obj, act
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = r.sub == p.sub && keyMatch2(r.obj, p.obj) && r.act == p.act
"""


def trail() -> tuple[FastAPIAudit, MemoryAuditSink]:
    sink = MemoryAuditSink()
    return FastAPIAudit(sinks=[sink]), sink


def summary(sink: MemoryAuditSink) -> list[tuple[str, str, Any]]:
    return [(event.action, event.outcome, event.actor) for event in sink.events]


class FromHeader(Strategy):
    """Finds the user of `X-User`, or fails with `X-User: wrong`; a login when `logs_in`."""

    def __init__(self, name: str = "header", logs_in: bool = False) -> None:
        super().__init__(name)
        self.logs_in = logs_in
        self.challenge = "Bearer"

    async def authenticate(self, connection: HTTPConnection) -> Any:
        user = connection.headers.get("x-user")
        if user == "wrong":
            raise AuthenticationFailed("a password with 'secret' in it")
        if user:
            self.grant(connection, ["orders:read"])
        return {"sub": user, "name": user.title()} if user else None


class TestAuthentication:
    def client(self, *strategies: Strategy, **options: Any) -> tuple[TestClient, MemoryAuditSink]:
        audit, sink = trail()
        auth = FastAPIAuth(*strategies, audit=audit, **options)
        app = FastAPI()

        reading = Security(auth.authenticate(), scopes=["orders:read"])

        @app.get("/orders/{order_id}", dependencies=[reading])
        async def order(order_id: int) -> int:
            return order_id

        @app.post("/orders", dependencies=[Security(auth.authenticate(), scopes=["orders:write"])])
        async def place() -> None: ...

        return TestClient(app), sink

    def test_failures_are_recorded_without_their_message(self) -> None:
        client, sink = self.client(FromHeader())
        assert client.get("/orders/1", headers={"X-User": "wrong"}).status_code == 401
        (event,) = sink.events
        assert (event.action, event.outcome, event.actor) == ("auth.failure", "failure", None)
        assert event.details == {"strategy": "header"}
        assert (event.route, event.method) == ("/orders/{order_id}", "GET")
        assert "secret" not in event.to_json()  # the message may carry what the client sent

    def test_requests_authenticated_and_anonymous_ones_arent(self) -> None:
        client, sink = self.client(FromHeader())
        assert client.get("/orders/1", headers={"X-User": "ana"}).status_code == 200
        assert client.get("/orders/1").status_code == 401
        assert not sink.events

    def test_logins_are(self) -> None:
        client, sink = self.client(FromHeader("github", logs_in=True))
        client.get("/orders/1", headers={"X-User": "ana"})
        assert summary(sink) == [("auth.login", "success", "ana")]
        assert sink.events[0].details == {"strategy": "github"}

    def test_a_missing_scope(self) -> None:
        client, sink = self.client(FromHeader())
        assert client.post("/orders", headers={"X-User": "ana"}).status_code == 403
        (event,) = sink.events
        assert (event.action, event.outcome, event.actor) == ("auth.scope", "denied", "ana")
        assert event.details == {"strategy": "header", "missing": ["orders:write"]}

    def test_the_actor(self) -> None:
        client, sink = self.client(FromHeader(), actor=lambda user: f"user:{user['sub']}")
        client.post("/orders", headers={"X-User": "ana"})
        assert sink.events[0].actor == "user:ana"

    @pytest.mark.parametrize(
        ("user", "actor"),
        [
            ("ana", "ana"),
            (7, 7),
            (uuid.UUID(int=1), uuid.UUID(int=1)),
            ({"sub": "u1", "id": "x"}, "u1"),
            ({"id": 3}, 3),
            (SimpleNamespace(id="u2"), "u2"),
            (SimpleNamespace(id=object), str(object)),
            ({"name": "nobody"}, None),
            (True, None),
            ({"sub": True}, None),
        ],
    )
    def test_the_default_actor(self, user: Any, actor: Any) -> None:
        assert _actor(user) == actor


def user_header(x_user: Annotated[str | None, Header()] = None) -> str | None:
    return x_user


def policy(*rules: list[str]) -> casbin.Enforcer:
    model = casbin.Model()
    model.load_model_from_text(RBAC)
    enforcer = casbin.Enforcer(model)
    enforcer.add_policies(list(rules))
    return enforcer


class Down:
    async def enforce(self, *values: Any) -> bool:
        raise PolicyUnavailable("the policy service doesn't answer")


def authz_app(enforcer: Any, **options: Any) -> tuple[TestClient, MemoryAuditSink, FastAPIAuthz]:
    audit, sink = trail()
    authz = FastAPIAuthz(enforcer, subject=user_header, audit=audit, **options)
    app = FastAPI()

    @app.get("/articles/{article_id}", dependencies=[Depends(authz)])
    async def article(article_id: int) -> int:
        return article_id

    publishing = Depends(authz.require("articles", "publish"))

    @app.post("/articles/{article_id}/publish", dependencies=[publishing])
    async def publish(article_id: int) -> int:
        return article_id

    @app.get("/drafts")
    async def drafts(user: Annotated[str | None, Depends(user_header)]) -> bool:
        return await authz.enforce(user, "drafts", "read")

    return TestClient(app), sink, authz


class TestAuthorization:
    def test_refusals_are_recorded(self) -> None:
        client, sink, _ = authz_app(policy(["ana", "/articles/:id", "GET"]))
        assert client.get("/articles/1", headers={"X-User": "ana"}).status_code == 200
        assert client.get("/articles/1", headers={"X-User": "ben"}).status_code == 403
        assert client.post("/articles/1/publish", headers={"X-User": "ana"}).status_code == 403
        events: list[AuditEvent] = list(sink.events)
        assert [(e.action, e.outcome, e.actor, e.resource) for e in events] == [
            ("authz.denied", "denied", "ben", "/articles/1"),
            ("authz.denied", "denied", "ana", "articles"),
        ]
        assert events[0].details == {"request": ["/articles/1", "GET"], "status": 403}
        assert (events[0].reason, events[0].route) == ("Forbidden", "/articles/{article_id}")

    def test_what_isnt_recorded(self) -> None:
        """Requests without a subject (authentication's), and enforce() in a route (the route's
        to decide)."""
        client, sink, _ = authz_app(policy(["ana", "/articles/:id", "GET"]))
        assert client.get("/articles/1").status_code == 401
        assert client.get("/drafts", headers={"X-User": "ana"}).json() is False
        assert not sink.events

    def test_anonymous_refusals_arent_either(self) -> None:
        client, sink, _ = authz_app(policy(), anonymous="*anonymous")
        assert client.get("/articles/1").status_code == 401
        assert not sink.events

    def test_control_characters(self) -> None:
        client, sink, _ = authz_app(policy(["ana", "/articles/:id", "GET"]))
        assert client.get("/articles/1%0A", headers={"X-User": "ana"}).status_code == 400
        assert summary(sink) == [("authz.denied", "denied", "ana")]
        assert sink.events[0].reason == "Invalid path"

    def test_an_engine_that_doesnt_answer(self) -> None:
        client, sink, _ = authz_app(Down())
        assert client.get("/articles/1", headers={"X-User": "ana"}).status_code == 503
        assert summary(sink) == [("authz.unavailable", "failure", "ana")]

    def test_the_actor(self) -> None:
        client, sink, _ = authz_app(policy(), actor=lambda subject: f"user:{subject}")
        client.get("/articles/1", headers={"X-User": "ben"})
        assert sink.events[0].actor == "user:ben"

    @pytest.mark.parametrize(
        ("subject", "actor"),
        [
            ("ana", "ana"),
            ({"uid": {"type": "User", "id": "ana"}, "attrs": {}}, "ana"),  # a Cedar entity
            ({"name": "ana", "roles": ["support"]}, "ana"),
            ({"sub": "u1"}, "u1"),
            (SimpleNamespace(identity="u2"), "u2"),
            (SimpleNamespace(id=5), 5),
            ({"roles": []}, None),
            (False, None),
        ],
    )
    def test_subject_ids(self, subject: Any, actor: Any) -> None:
        assert subject_id(subject) == actor


class BasicAuth(AuthenticationBackend):
    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, SimpleUser] | None:
        if "Authorization" not in conn.headers:
            return None
        username = base64.b64decode(conn.headers["Authorization"].split()[1]).decode().split(":")[0]
        return AuthCredentials(["authenticated"]), SimpleUser(username)


def test_the_middleware() -> None:
    audit, sink = trail()
    app = FastAPI()
    app.get("/articles/{article_id}")(lambda article_id: article_id)
    enforcer = policy(["ana", "/articles/:id", "GET"])
    app.add_middleware(AuthzMiddleware, enforcer=enforcer, audit=audit)
    app.add_middleware(AuthenticationMiddleware, backend=BasicAuth())
    client = TestClient(app)
    assert client.get("/articles/1", auth=("ana", "x")).status_code == 200
    assert client.get("/articles/1", auth=("ben", "x")).status_code == 403
    assert client.get("/articles/1").status_code == 401
    assert summary(sink) == [("authz.denied", "denied", "ben")]
    assert sink.events[0].resource == "/articles/1"
