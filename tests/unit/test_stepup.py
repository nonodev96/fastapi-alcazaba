"""Step-up by factor: sessions marked with a method, tokens with auth_time, amr and acr and RFC
9470's challenge, the problem the front end reads, and the audit trail."""

import time
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from fastapi_alcazaba import Cache, FastAPIAuth
from fastapi_alcazaba.audit import FastAPIAudit, MemoryAuditSink
from fastapi_alcazaba.auth import stepup
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.observability import FastAPIProblemDetails
from fastapi_alcazaba.security import ServerSessionMiddleware

JWT_SECRET = "jwt-secret-of-the-step-up-tests!"  # noqa: S105  # the tests' own


def session_app(**options: Any) -> TestClient:
    app = FastAPI()
    FastAPIProblemDetails().install(app)
    Cache(InMemoryBackend()).install(app)
    app.add_middleware(ServerSessionMiddleware, cookie_name="session", secure=False)

    def current_user(request: Request) -> str | None:
        user = request.session.get("user")
        return str(user) if user is not None else None

    @app.post("/login/{method}")
    async def login(request: Request, method: str, at: float | None = None) -> None:
        request.session["user"] = "ana"
        stepup.mark(request, "ana", method)
        if at is not None:
            request.session[stepup._SESSION_KEY]["at"] = at  # pylint: disable=protected-access

    @app.delete("/account")
    async def delete_account(
        user: Annotated[str, Depends(stepup.require_recent(current_user, **options))],
    ) -> dict[str, str]:
        return {"deleted": user}

    @app.get("/method")
    async def method(request: Request) -> dict[str, Any]:
        return {"method": stepup.verified_with(request, "ana")}

    return TestClient(app)


class TestSessions:
    def test_a_method_among_those_wanted(self) -> None:
        client = session_app(methods={"passkey"})
        client.post("/login/totp")
        refused = client.delete("/account")
        assert refused.status_code == 403
        assert refused.headers["content-type"] == "application/problem+json"
        assert refused.json() == {
            "type": stepup.PROBLEM_TYPE,
            "title": "Step-up required",
            "status": 403,
            "detail": "Second factor required",
            "instance": "/account",
            "max_age": 300,
            "methods": ["passkey"],
        }
        client.post("/login/passkey")
        assert client.delete("/account").json() == {"deleted": "ana"}
        assert client.get("/method").json() == {"method": "passkey"}

    def test_any_method_without_methods(self) -> None:
        client = session_app()
        client.post("/login/totp")
        assert client.delete("/account").status_code == 200

    def test_too_old(self) -> None:
        client = session_app(max_age=60)
        client.post("/login/passkey", params={"at": time.time() - 61})
        assert client.delete("/account").status_code == 403

    def test_no_mark_no_user(self) -> None:
        client = session_app()
        assert client.delete("/account").status_code == 401
        assert client.get("/method").json() == {"method": None}

    @pytest.mark.parametrize(
        "options",
        [{"max_age": 0}, {"methods": []}, {"acr_values": set()}],
    )
    def test_invalid(self, options: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="max_age|at least one"):
            stepup.require_recent(lambda: None, **options)


def token_app(audit: FastAPIAudit | None = None, **options: Any) -> tuple[TestClient, JWTStrategy]:
    app = FastAPI()
    FastAPIProblemDetails().install(app)
    tokens = JWTStrategy(JWT_SECRET)
    auth = FastAPIAuth(tokens)
    claims = auth.authenticate("jwt")

    def user_id(token: Annotated[dict[str, Any], Depends(claims)]) -> str:
        return str(token["sub"])

    @app.post("/transfers")
    async def transfer(
        user: Annotated[
            str,
            Depends(stepup.require_recent(user_id, claims=claims, audit=audit, **options)),
        ],
    ) -> dict[str, str]:
        return {"by": user}

    return TestClient(app), tokens


def bearer(tokens: JWTStrategy, **claims: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.issue('ana', **claims)}"}


class TestTokens:
    def test_recent_with_the_method_and_the_acr(self) -> None:
        client, tokens = token_app(max_age=120, methods={"hwk"}, acr_values={"urn:mfa"})
        good = bearer(tokens, auth_time=int(time.time()) - 10, amr=["pwd", "hwk"], acr="urn:mfa")
        assert client.post("/transfers", headers=good).json() == {"by": "ana"}

    @pytest.mark.parametrize(
        "claims",
        [
            {},  # no auth_time
            {"auth_time": "yesterday"},
            {"auth_time": True},
            {"auth_time": 1, "amr": ["hwk"], "acr": "urn:mfa"},  # too old
            {"auth_time": "now", "amr": ["otp"], "acr": "urn:mfa"},  # another method
            {"auth_time": "now", "amr": "hwk", "acr": "urn:mfa"},  # amr isn't a list
            {"auth_time": "now", "amr": ["hwk"], "acr": "urn:pwd"},  # another level
        ],
    )
    def test_rfc_9470_challenge(self, claims: dict[str, Any]) -> None:
        client, tokens = token_app(max_age=120, methods={"hwk"}, acr_values={"urn:mfa"})
        if claims.get("auth_time") == "now":
            claims["auth_time"] = int(time.time())
        refused = client.post("/transfers", headers=bearer(tokens, **claims))
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == (
            'Bearer error="insufficient_user_authentication", '
            'error_description="A more recent or stronger authentication is required", '
            'max_age="120", acr_values="urn:mfa"'
        )
        body = refused.json()
        assert (body["type"], body["max_age"], body["acr_values"]) == (
            stepup.PROBLEM_TYPE,
            120,
            ["urn:mfa"],
        )

    def test_without_acr_values(self) -> None:
        client, tokens = token_app(max_age=60)
        refused = client.post("/transfers", headers=bearer(tokens))
        assert "acr_values" not in refused.headers["www-authenticate"]
        assert refused.json()["acr_values"] is None
        fresh = bearer(tokens, auth_time=int(time.time()))
        assert client.post("/transfers", headers=fresh).status_code == 200


class TestAudit:
    async def test_each_check_is_recorded(self) -> None:
        sink = MemoryAuditSink()
        client, tokens = token_app(audit=FastAPIAudit(sinks=[sink]), methods={"hwk"})
        client.post("/transfers", headers=bearer(tokens, auth_time=int(time.time()), amr=["hwk"]))
        client.post("/transfers", headers=bearer(tokens, auth_time=int(time.time()), amr=["pwd"]))
        events = [
            (event.action, event.outcome, event.reason, event.details["method"])
            for event in sink.events
        ]
        assert events == [
            ("auth.stepup", "success", None, "hwk"),
            ("auth.stepup", "failure", "wrong_method", "pwd"),
        ]
        assert all(event.actor == "ana" for event in sink.events)


class FakeConnection:
    """What stepup reads of a request: its scope's session."""

    def __init__(self, session: dict[str, Any]) -> None:
        self.scope = {"session": session}


@pytest.mark.parametrize(
    ("mark", "at", "method"),
    [
        ({"user": "ana", "at": 1.5, "method": "totp"}, 1.5, "totp"),
        ({"user": "ana", "at": True, "method": "totp"}, None, None),  # not a time
        ({"user": "ana", "at": 2, "method": 7}, 2.0, None),  # not a method
        ({"user": "ben", "at": 2, "method": "totp"}, None, None),  # someone else's
        ("not a mark", None, None),
    ],
)
def test_marks_as_the_session_holds_them(mark: Any, at: float | None, method: str | None) -> None:
    connection: Any = FakeConnection({stepup._SESSION_KEY: mark})  # pylint: disable=protected-access
    assert stepup.verified_at(connection, "ana") == at
    assert stepup.verified_with(connection, "ana") == method


def test_without_sessions() -> None:
    connection: Any = FakeConnection({})
    connection.scope = {}
    with pytest.raises(RuntimeError, match="needs ServerSessionMiddleware"):
        stepup.verified_at(connection, "ana")
