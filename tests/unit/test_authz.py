"""FastAPICasbin and CasbinMiddleware: Casbin's policy decides, per route or for every request."""

import base64
import binascii
import logging
from typing import Annotated, Any

import casbin
import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import FastAPICasbin, FastAPIProblemDetails
from fastapi_alcazaba.authz import WEBSOCKET, CasbinMiddleware
from fastapi_alcazaba.telemetry.metrics import Metrics

RBAC = """
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub) && keyMatch2(r.obj, p.obj) && r.act == p.act
"""

POLICY = [
    ["reader", "/articles", "GET"],
    ["reader", "/articles/:id", "GET"],
    ["editor", "/articles/:id", "PUT"],
    ["editor", "articles", "publish"],
    ["alice", "/ws", WEBSOCKET],
    ["anonymous", "/catalog", "GET"],
]
ROLES = [["alice", "editor"], ["editor", "reader"], ["bob", "reader"]]


def model(text: str = RBAC) -> casbin.Model:
    loaded = casbin.Model()
    loaded.load_model_from_text(text)
    return loaded


def rbac() -> casbin.Enforcer:
    enforcer = casbin.Enforcer(model())
    enforcer.add_policies(POLICY)
    enforcer.add_grouping_policies(ROLES)
    return enforcer


def as_user(name: str) -> dict[str, str]:
    return {"X-User": name}


def user_header(x_user: Annotated[str | None, Header()] = None) -> str | None:
    """Stands in for the app's authentication: who the request says it is."""
    return x_user


def articles_app(authz: FastAPICasbin) -> FastAPI:
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(authz)])

    @router.get("/articles")
    async def articles() -> list[str]:
        return ["first"]

    @router.get("/articles/{article_id}")
    async def article(article_id: int) -> int:
        return article_id

    @router.put("/articles/{article_id}")
    async def update(article_id: int) -> int:
        return article_id

    @router.get("/catalog")
    async def catalog() -> list[str]:
        return ["olive oil"]

    app.include_router(router)

    @app.post("/articles/{article_id}/publish")
    async def publish(
        article_id: int,
        user: Annotated[str | None, Depends(authz.require("articles", "publish"))],
    ) -> dict[str, Any]:
        return {"article": article_id, "by": user}

    return app


@pytest.fixture(name="client")
def fixture_client() -> TestClient:
    return TestClient(articles_app(FastAPICasbin(rbac(), subject=user_header)))


class TestPathAndMethod:
    def test_the_policy_decides(self, client: TestClient) -> None:
        assert client.get("/articles/1", headers=as_user("alice")).status_code == 200
        assert client.put("/articles/1", headers=as_user("alice")).status_code == 200
        assert client.get("/articles/1", headers=as_user("bob")).status_code == 200
        refused = client.put("/articles/1", headers=as_user("bob"))
        assert (refused.status_code, refused.json()) == (403, {"detail": "Forbidden"})
        assert "www-authenticate" not in refused.headers

    def test_unknown_subjects_are_refused(self, client: TestClient) -> None:
        assert client.get("/articles", headers=as_user("mallory")).status_code == 403

    def test_without_a_subject_a_401_without_asking_the_policy(self, client: TestClient) -> None:
        refused = client.get("/catalog")  # open to "anonymous", which isn't set
        assert (refused.status_code, refused.json()) == (401, {"detail": "Not authenticated"})
        assert refused.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("path", ["/articles/%0A", "/articles/1%0A", "/catalog%0A"])
    def test_control_characters_are_refused(self, client: TestClient, path: str) -> None:
        """Starlette routes "/articles/\n" to "/articles/{article_id}" (its patterns end in "$",
        which matches before a final newline), where keyMatch2 would read the newline as an id."""
        refused = client.get(path, headers=as_user("bob"))
        assert (refused.status_code, refused.json()) == (400, {"detail": "Invalid path"})
        assert client.get(path).status_code == 400

    def test_a_deny_rule_is_not_escaped(self) -> None:
        deny = (
            RBAC.replace("p = sub, obj, act", "p = sub, obj, act, eft")
            .replace(
                "e = some(where (p.eft == allow))",
                "e = some(where (p.eft == allow)) && !some(where (p.eft == deny))",
            )
            .replace("keyMatch2(r.obj, p.obj)", "(p.obj == '*' || r.obj == p.obj)")
        )
        enforcer = casbin.Enforcer(model(deny))
        enforcer.add_policies([["bob", "*", "GET", "allow"], ["bob", "/articles", "GET", "deny"]])
        client = TestClient(articles_app(FastAPICasbin(enforcer, subject=user_header)))
        assert client.get("/articles", headers=as_user("bob")).status_code == 403
        assert client.get("/articles%0A", headers=as_user("bob")).status_code == 400

    def test_the_path_is_the_one_the_app_routes(self) -> None:
        seen: list[tuple[Any, ...]] = []

        class Recorder:
            def enforce(self, *values: Any) -> bool:
                seen.append(values)
                return True

        api = articles_app(FastAPICasbin(Recorder(), subject=user_header))
        app = FastAPI()
        app.mount("/api", api)
        client = TestClient(app, root_path="/v1")
        assert client.get("/v1/api/articles/7", headers=as_user("alice")).status_code == 200
        assert seen == [("alice", "/articles/7", "GET")]


class TestAnonymous:
    def test_requests_without_a_subject_are_checked_as_anonymous(self) -> None:
        authz = FastAPICasbin(rbac(), subject=user_header, anonymous="anonymous", challenge="Basic")
        client = TestClient(articles_app(authz))
        assert client.get("/catalog").json() == ["olive oil"]
        refused = client.get("/articles")
        assert (refused.status_code, refused.json()) == (401, {"detail": "Not authenticated"})
        assert refused.headers["www-authenticate"] == "Basic"

    def test_anonymous_is_a_name(self) -> None:
        with pytest.raises(ValueError, match="anonymous"):
            FastAPICasbin(rbac(), anonymous="")


class TestRequire:
    def test_own_objects_and_actions(self, client: TestClient) -> None:
        published = client.post("/articles/3/publish", headers=as_user("alice"))
        assert published.json() == {"article": 3, "by": "alice"}
        refused = client.post("/articles/3/publish", headers=as_user("bob"))
        assert (refused.status_code, refused.json()) == (403, {"detail": "Forbidden"})
        assert client.post("/articles/3/publish").status_code == 401

    def test_returns_none_for_anonymous_requests_let_through(self) -> None:
        enforcer = rbac()
        enforcer.add_policy("anonymous", "articles", "read")
        authz = FastAPICasbin(enforcer, subject=user_header, anonymous="anonymous")
        app = FastAPI()

        @app.get("/articles")
        async def articles(
            user: Annotated[str | None, Depends(authz.require("articles", "read"))],
        ) -> dict[str, str | None]:
            return {"user": user}

        client = TestClient(app)
        assert client.get("/articles").json() == {"user": None}
        assert client.get("/articles", headers=as_user("bob")).status_code == 403

    def test_braces_take_path_parameters(self) -> None:
        enforcer = rbac()
        # No colon in the names: keyMatch2 reads ":7" as a parameter.
        enforcer.add_policies([["ana", "projects/7", "delete"], ["ana", "{literal}", "read"]])
        authz = FastAPICasbin(enforcer, subject=user_header)
        app = FastAPI()

        @app.delete("/projects/{project_id}")
        async def delete(
            project_id: int,
            user: Annotated[str, Depends(authz.require("projects/{project_id}", "delete"))],
        ) -> dict[str, Any]:
            return {"project": project_id, "by": user}

        @app.get("/literal", dependencies=[Depends(authz.require("{{literal}}", "read"))])
        async def literal() -> None:
            pass

        client = TestClient(app)
        deleted = client.delete("/projects/7", headers=as_user("ana"))
        assert deleted.json() == {"project": 7, "by": "ana"}
        assert client.delete("/projects/8", headers=as_user("ana")).status_code == 403
        assert client.get("/literal", headers=as_user("ana")).status_code == 200

    def test_a_missing_path_parameter_is_an_error(self) -> None:
        authz = FastAPICasbin(rbac(), subject=user_header)
        app = FastAPI()

        project = authz.require("projects/{project_id}", "list")

        @app.get("/projects", dependencies=[Depends(project)])
        async def projects() -> None:
            pass

        with pytest.raises(RuntimeError, match="'project_id', which isn't a path parameter"):
            TestClient(app).get("/projects", headers=as_user("alice"))

    @pytest.mark.parametrize(
        "values",
        [(), ("{a.b}",), ("{0}",), ("{}",), ("{x!r}",), ("{x:>3}",), ("}",), ("{",)],
    )
    def test_values_are_checked_when_declared(self, values: tuple[str, ...]) -> None:
        with pytest.raises(ValueError):  # noqa: PT011  # each has its own message
            FastAPICasbin(rbac()).require(*values)

    def test_values_are_strings(self) -> None:
        with pytest.raises(TypeError, match="strings, not int"):
            FastAPICasbin(rbac()).require("articles", 1)  # type: ignore[arg-type]


class TestSubject:
    def test_a_dependency_of_its_own(self) -> None:
        tokens = {"token-of-alice": "alice", "token-of-bob": "bob"}
        bearer = HTTPBearer(auto_error=False)

        async def current_user(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ) -> str | None:
            return tokens.get(credentials.credentials) if credentials else None

        client = TestClient(articles_app(FastAPICasbin(rbac(), subject=current_user)))
        alice = {"Authorization": "Bearer token-of-alice"}
        assert client.put("/articles/1", headers=alice).status_code == 200
        bob = {"Authorization": "Bearer token-of-bob"}
        assert client.put("/articles/1", headers=bob).status_code == 403
        operation = client.get("/openapi.json").json()["paths"]["/articles/{article_id}"]["put"]
        assert operation["security"] == [{"HTTPBearer": []}]

    def test_by_default_the_user_of_starlettes_authentication(self) -> None:
        app = articles_app(FastAPICasbin(rbac()))
        app.add_middleware(AuthenticationMiddleware, backend=BasicAuth())
        client = TestClient(app)
        assert client.put("/articles/1", auth=("alice", "secret")).status_code == 200
        assert client.put("/articles/1", auth=("bob", "secret")).status_code == 403
        assert client.put("/articles/1").status_code == 401

    def test_without_starlettes_authentication_the_default_is_an_error(self) -> None:
        client = TestClient(articles_app(FastAPICasbin(rbac())))
        with pytest.raises(RuntimeError, match="AuthenticationMiddleware, which isn't installed"):
            client.get("/articles")

    def test_any_object_for_abac(self) -> None:
        abac = RBAC.replace("g(r.sub, p.sub) && keyMatch2(r.obj, p.obj)", "r.sub.Age >= 18")
        enforcer = casbin.Enforcer(model(abac))
        enforcer.add_policy("adults", "-", "GET")

        class Person:
            def __init__(self, age: int) -> None:
                self.Age = age  # pylint: disable=invalid-name  # as the matcher spells it

        def person(age: Annotated[int, Header()]) -> Person:
            return Person(age)

        client = TestClient(articles_app(FastAPICasbin(enforcer, subject=person)))
        assert client.get("/articles", headers={"Age": "30"}).status_code == 200
        assert client.get("/articles", headers={"Age": "12"}).status_code == 403


class TestEnforcers:
    async def test_an_async_enforcer_sees_its_changes_at_once(self) -> None:
        enforcer = casbin.AsyncEnforcer(model())
        await enforcer.add_policies(POLICY)
        authz = FastAPICasbin(enforcer, subject=user_header)
        app = articles_app(authz)

        @app.post("/roles/{user}/{role}")
        async def grant(user: str, role: str) -> None:
            await enforcer.add_role_for_user(user, role)

        client = TestClient(app)
        assert client.get("/articles", headers=as_user("carol")).status_code == 403
        client.post("/roles/carol/reader")
        assert client.get("/articles", headers=as_user("carol")).status_code == 200

    def test_a_disabled_enforcer_lets_everything_through(self, client: TestClient) -> None:
        enforcer = rbac()
        enforcer.enable_enforce(False)
        client = TestClient(articles_app(FastAPICasbin(enforcer, subject=user_header)))
        assert client.put("/articles/1", headers=as_user("mallory")).status_code == 200

    def test_a_broken_policy_fails_closed(self) -> None:
        domains = RBAC.replace("r = sub, obj, act", "r = sub, dom, obj, act")
        client = TestClient(
            articles_app(FastAPICasbin(casbin.Enforcer(model(domains)), subject=user_header)),
            raise_server_exceptions=False,
        )
        assert client.get("/articles", headers=as_user("alice")).status_code == 500

    def test_problem_details(self) -> None:
        app = articles_app(FastAPICasbin(rbac(), subject=user_header))
        FastAPIProblemDetails().install(app)
        refused = TestClient(app).put("/articles/1", headers=as_user("bob"))
        assert refused.status_code == 403
        assert refused.headers["content-type"] == "application/problem+json"


class TestWebSockets:
    @pytest.fixture(name="ws_client")
    def fixture_ws_client(self) -> TestClient:
        authz = FastAPICasbin(rbac(), subject=user_header)
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(authz)])
        async def chat(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text("hello")
            await websocket.close()

        return TestClient(app)

    def test_checked_as_websocket(self, ws_client: TestClient) -> None:
        with ws_client.websocket_connect("/ws", headers=as_user("alice")) as websocket:
            assert websocket.receive_text() == "hello"

    @pytest.mark.parametrize("headers", [as_user("bob"), {}])
    def test_refused_with_1008(self, ws_client: TestClient, headers: dict[str, str]) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            ws_client.websocket_connect("/ws", headers=headers),
        ):
            pass
        assert refused.value.code == 1008


class BasicAuth(AuthenticationBackend):
    """Stands in for the app's authentication: any password will do."""

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, SimpleUser] | None:
        if "Authorization" not in conn.headers:
            return None
        try:
            scheme, credentials = conn.headers["Authorization"].split()
            username, _, _ = base64.b64decode(credentials).decode().partition(":")
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise AuthenticationError("Invalid basic auth credentials") from exc
        if scheme.lower() != "basic":
            return None
        return AuthCredentials(["authenticated"]), SimpleUser(username)


def middleware_app(**options: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/articles")
    async def articles() -> list[str]:
        return ["first"]

    @app.put("/articles/{article_id}")
    async def update(article_id: int) -> int:
        return article_id

    @app.get("/catalog")
    async def catalog() -> list[str]:
        return ["olive oil"]

    @app.websocket("/ws")
    async def chat(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app.add_middleware(CasbinMiddleware, enforcer=options.pop("enforcer", rbac()), **options)
    app.add_middleware(AuthenticationMiddleware, backend=BasicAuth())
    return app


class TestMiddleware:
    def test_the_policy_decides(self) -> None:
        client = TestClient(middleware_app())
        assert client.put("/articles/1", auth=("alice", "secret")).status_code == 200
        refused = client.put("/articles/1", auth=("bob", "secret"))
        assert (refused.status_code, refused.json()) == (403, {"detail": "Forbidden"})

    def test_unauthenticated_requests_are_anonymous(self) -> None:
        client = TestClient(middleware_app(challenge="Basic"))
        assert client.get("/catalog").json() == ["olive oil"]
        refused = client.get("/articles")
        assert (refused.status_code, refused.json()) == (401, {"detail": "Not authenticated"})
        assert refused.headers["www-authenticate"] == "Basic"

    def test_or_refused_without_asking_the_policy(self) -> None:
        assert TestClient(middleware_app(anonymous=None)).get("/catalog").status_code == 401

    def test_options_are_checked_too(self) -> None:
        client = TestClient(middleware_app())
        assert client.options("/articles").status_code == 401

    def test_websockets(self) -> None:
        client = TestClient(middleware_app())
        alice = {"Authorization": "Basic " + base64.b64encode(b"alice:secret").decode()}
        with client.websocket_connect("/ws", headers=alice) as websocket:
            assert websocket.receive_text() == "hello"
        with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect("/ws"):
            pass
        assert refused.value.code == 1008

    def test_the_path_is_the_one_the_app_routes(self) -> None:
        app = FastAPI()
        app.mount("/api", middleware_app())
        client = TestClient(app)
        assert client.put("/api/articles/1", auth=("alice", "secret")).status_code == 200

    @pytest.mark.parametrize("path", ["/articles%0A", "/articles/1%0A", "/catalog%00"])
    def test_control_characters_are_refused(self, path: str) -> None:
        """Starlette routes "/articles\n" to "/articles", which the policy would read otherwise."""
        client = TestClient(middleware_app())
        refused = client.put(path, auth=("bob", "secret"))
        assert (refused.status_code, refused.json()) == (400, {"detail": "Invalid path"})
        assert client.get(path).status_code == 400

    def test_problem_details(self) -> None:
        app = middleware_app()
        FastAPIProblemDetails().install(app)
        refused = TestClient(app).put("/articles/1", auth=("bob", "secret"))
        assert refused.status_code == 403
        assert refused.headers["content-type"] == "application/problem+json"
        assert refused.json()["title"] == "Forbidden"

    def test_needs_starlettes_authentication(self) -> None:
        app = FastAPI()
        app.add_middleware(CasbinMiddleware, enforcer=rbac())
        with pytest.raises(RuntimeError, match="AuthenticationMiddleware"):
            TestClient(app).get("/articles")

    def test_lifespan_goes_through(self) -> None:
        with TestClient(middleware_app()) as client:
            assert client.get("/catalog").status_code == 200


def test_decisions_are_counted(caplog: pytest.LogCaptureFixture) -> None:
    reader = InMemoryMetricReader()
    metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
    client = TestClient(articles_app(FastAPICasbin(rbac(), subject=user_header, metrics=metrics)))
    with caplog.at_level(logging.DEBUG, logger="fastapi_alcazaba.authz.enforcer"):
        client.get("/articles", headers=as_user("bob"))
        client.put("/articles/1", headers=as_user("bob"))
        client.get("/articles")
    data = reader.get_metrics_data()
    assert data is not None
    counted = {
        (point.attributes or {}).get("fastapi_alcazaba.outcome"): getattr(point, "value", None)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "fastapi_alcazaba.authz"
        for point in metric.data.data_points
    }
    assert counted == {"allowed": 1, "denied": 1, "unauthenticated": 1}
    assert [record.getMessage() for record in caplog.records] == [
        "Casbin refused ('bob', '/articles/1', 'PUT')"
    ]
