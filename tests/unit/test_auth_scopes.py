"""Scopes: routes that need some (FastAPI's `Security(..., scopes=[...])`, or `authenticate(...,
scopes=)`), the strategies that grant those of their credentials, and a request authenticated
once whatever the dependencies that ask."""

from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Security, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba.auth import (
    AuthenticationFailed,
    FastAPIAuth,
    HTTPBearerStrategy,
    Strategy,
    granted_scopes,
)
from fastapi_alcazaba.auth.strategies import JWTStrategy

SECRET = "jwt-secret-of-at-least-32-bytes!!"  # noqa: S105  # the tests' own


class Granting(Strategy):
    """Finds `user` and grants `scopes`, or fails after granting them."""

    def __init__(self, name: str, user: Any, scopes: Any = (), fail: bool = False) -> None:
        super().__init__(name)
        self.user, self.scopes, self.fail, self.calls = user, scopes, fail, 0
        self.challenge = "Bearer"

    async def authenticate(self, connection: HTTPConnection) -> Any:
        self.calls += 1
        if self.scopes is not None:
            self.grant(connection, self.scopes)
        if self.fail:
            raise AuthenticationFailed("granted, then refused")
        return self.user


def app_with(auth: FastAPIAuth, *names: str) -> TestClient:
    app = FastAPI()
    authenticated = auth.authenticate(*names)

    @app.get("/orders", dependencies=[Security(authenticated, scopes=["orders:read"])])
    async def orders() -> list[str]:
        return ["order 1"]

    @app.post("/orders")
    async def place(
        user: Annotated[Any, Security(authenticated, scopes=["orders:write"])],
        again: Annotated[Any, Depends(authenticated)],
    ) -> Any:
        assert user is again
        return user

    @app.get("/admin", dependencies=[Depends(auth.authenticate(*names, scopes=["admin"]))])
    async def admin() -> str:
        return "admin"

    @app.get("/scopes")
    async def scopes(
        connection: HTTPConnection,
        user: Annotated[Any, Depends(authenticated)],
    ) -> list[str] | None:
        """What the user's credentials grant; null when its strategy grants none."""
        del user
        granted = granted_scopes(connection)
        return None if granted is None else sorted(granted)

    return TestClient(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestScopes:
    def test_granted_scopes_let_through(self) -> None:
        jwt = JWTStrategy(SECRET)
        client = app_with(FastAPIAuth(jwt))
        token = jwt.issue("ana", scope="orders:read orders:write")
        assert client.get("/orders", headers=bearer(token)).json() == ["order 1"]
        assert client.post("/orders", headers=bearer(token)).json()["sub"] == "ana"
        granted = client.get("/scopes", headers=bearer(token)).json()
        assert granted == ["orders:read", "orders:write"]

    def test_a_missing_scope_is_a_403(self) -> None:
        jwt = JWTStrategy(SECRET)
        client = app_with(FastAPIAuth(jwt))
        token = jwt.issue("ana", scope="orders:read")
        refused = client.post("/orders", headers=bearer(token))
        assert (refused.status_code, refused.json()) == (403, {"detail": "Insufficient scope"})
        assert refused.headers["WWW-Authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write"'
        )
        assert client.get("/admin", headers=bearer(token)).status_code == 403

    @pytest.mark.parametrize(
        ("claims", "granted"),
        [
            ({"scp": ["orders:read", "admin"]}, ["admin", "orders:read"]),  # Entra ID, Okta
            ({"scope": "a", "scp": "b c"}, ["a", "b", "c"]),
            ({}, []),  # a JWT without scopes grants none, but it's a strategy that grants
        ],
    )
    def test_the_claims_that_grant_them(self, claims: dict[str, Any], granted: list[str]) -> None:
        jwt = JWTStrategy(SECRET)
        client = app_with(FastAPIAuth(jwt))
        response = client.get("/scopes", headers=bearer(jwt.issue("ana", **claims)))
        assert response.json() == granted

    def test_other_claims(self) -> None:
        jwt = JWTStrategy(SECRET, scope_claims=["permissions"])  # Auth0's
        client = app_with(FastAPIAuth(jwt))
        token = jwt.issue("ana", permissions=["admin"], scope="orders:read")
        assert client.get("/admin", headers=bearer(token)).status_code == 200
        assert client.get("/orders", headers=bearer(token)).status_code == 403

    @pytest.mark.parametrize("scope", [42, ["a b"], ["ok", 7], {"a": 1}])
    def test_scopes_that_arent_scope_tokens_refuse_the_token(self, scope: Any) -> None:
        jwt = JWTStrategy(SECRET)
        client = app_with(FastAPIAuth(jwt))
        token = jwt.issue("ana", scope=scope)
        assert client.get("/scopes", headers=bearer(token)).status_code == 401

    def test_bearer_tokens_with_scopes_of(self) -> None:
        tokens = {"pat_1": {"id": "ana", "scopes": ["orders:read"]}}
        strategy = HTTPBearerStrategy(tokens.get, scopes_of=lambda user: user["scopes"])
        client = app_with(FastAPIAuth(strategy))
        assert client.get("/orders", headers=bearer("pat_1")).status_code == 200
        assert client.post("/orders", headers=bearer("pat_1")).status_code == 403

    def test_a_strategy_that_grants_none(self) -> None:
        """A session's user has no scopes: a route that needs some refuses it, without a Bearer
        challenge."""

        class Session(Strategy):
            async def authenticate(self, connection: HTTPConnection) -> Any:
                return "ana"

        client = app_with(FastAPIAuth(Session("session")))
        refused = client.get("/orders")
        assert refused.status_code == 403
        assert "WWW-Authenticate" not in refused.headers

    def test_only_the_strategy_that_found_the_user_grants(self) -> None:
        """A strategy that granted scopes, then failed: its scopes aren't the next one's."""
        failing = Granting("first", "ana", ["orders:write"], fail=True)
        finding = Granting("second", "ben", None)
        client = app_with(FastAPIAuth(failing, finding))
        assert client.post("/orders").status_code == 403
        assert client.get("/scopes").json() is None

    def test_without_a_user_scopes_dont_matter(self) -> None:
        auth = FastAPIAuth(Granting("nobody", None))
        app = FastAPI()

        @app.get("/maybe")
        async def maybe(
            user: Annotated[Any, Security(auth.authenticate(optional=True), scopes=["x"])],
        ) -> Any:
            return user

        assert TestClient(app).get("/maybe").json() is None
        assert app_with(auth).get("/orders").status_code == 401

    def test_websockets(self) -> None:
        auth = FastAPIAuth(Granting("key", "ana", ["chat"]))
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Security(auth.authenticate(), scopes=["admin"])])
        async def chat(websocket: WebSocket) -> None:
            await websocket.accept()

        with (
            pytest.raises(WebSocketDisconnect) as refused,
            TestClient(app).websocket_connect("/ws"),
        ):
            pass
        assert refused.value.code == 1008

    def test_in_the_openapi_schema(self) -> None:
        client = app_with(FastAPIAuth(JWTStrategy(SECRET)))
        paths = client.get("/openapi.json").json()["paths"]
        assert paths["/orders"]["post"]["security"] == [{"jwt": ["orders:write"]}]
        assert paths["/orders"]["get"]["security"] == [{"jwt": ["orders:read"]}]

    def test_configuration(self) -> None:
        auth = FastAPIAuth(Granting("key", "ana"))
        with pytest.raises(TypeError, match="not a string"):
            auth.authenticate(scopes="admin")
        with pytest.raises(ValueError, match="without spaces"):
            auth.authenticate(scopes=["a b"])


def test_a_request_is_authenticated_once() -> None:
    """Two dependencies, one with scopes: the strategy runs once (an API key verified once)."""
    strategy = Granting("key", "ana", ["orders:write"])
    client = app_with(FastAPIAuth(strategy))
    assert client.post("/orders").status_code == 200
    assert strategy.calls == 1
    client.get("/orders")
    assert strategy.calls == 2  # once per request
