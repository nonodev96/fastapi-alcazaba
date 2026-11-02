"""CedarEnforcer: Cedar's policies decide for FastAPIAuthz and AuthzMiddleware, and a policy that
can't be evaluated refuses."""

import base64
import logging
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.testclient import TestClient
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection

from fastapi_alcazaba.authz import AuthzMiddleware, FastAPIAuthz
from fastapi_alcazaba.authz.cedar import CedarEnforcer

POLICIES = """
permit(principal in Role::"reader", action == Action::"GET", resource)
when { resource.id like "/articles/*" };
permit(principal in Role::"editor", action == Action::"PUT", resource)
when { resource.id like "/articles/*" };
permit(principal in Role::"editor", action == Action::"publish", resource == Resource::"articles");
forbid(principal, action, resource) when { resource has locked && resource.locked };
permit(principal == User::"*anonymous", action == Action::"GET", resource == Resource::"/catalog");
"""

ROLES: list[dict[str, Any]] = [
    {"uid": {"type": "Role", "id": "reader"}, "attrs": {}, "parents": []},
    {
        "uid": {"type": "Role", "id": "editor"},
        "attrs": {},
        "parents": [{"type": "Role", "id": "reader"}],
    },
    {
        "uid": {"type": "Resource", "id": "archive"},
        "attrs": {"id": "archive", "locked": True},
        "parents": [],
    },
]

USERS = {"ana": "editor", "bob": "reader"}


def user_entities(subject: str) -> list[dict[str, Any]]:
    """The user and its role, as the app's database would give them."""
    role = USERS.get(subject)
    parents = [{"type": "Role", "id": role}] if role else []
    return [{"uid": {"type": "User", "id": subject}, "attrs": {}, "parents": parents}]


def enforcer(**options: Any) -> CedarEnforcer:
    return CedarEnforcer(POLICIES, entities=ROLES, entities_of=user_entities, **options)


class TestEnforce:
    async def test_roles_and_their_hierarchy(self) -> None:
        cedar = enforcer()
        assert await cedar.enforce("ana", "articles", "publish")
        assert not await cedar.enforce("bob", "articles", "publish")
        assert await cedar.enforce("ana", "/articles/7", "GET")  # editors are readers
        assert await cedar.enforce("bob", "/articles/7", "GET")
        assert not await cedar.enforce("bob", "/articles/7", "PUT")
        assert not await cedar.enforce("mallory", "/articles/7", "GET")  # no role

    async def test_paths_by_their_id(self) -> None:
        cedar = enforcer()
        assert not await cedar.enforce("bob", "/users/7", "GET")
        assert await cedar.enforce("*anonymous", "/catalog", "GET")
        assert not await cedar.enforce("*anonymous", "/catalog/x", "GET")

    async def test_forbid_beats_permit(self) -> None:
        cedar = CedarEnforcer("permit(principal, action, resource);\n" + POLICIES, entities=ROLES)
        assert await cedar.enforce("ana", "articles", "read")
        assert not await cedar.enforce("ana", "archive", "read")

    async def test_a_policy_that_cant_be_evaluated_refuses(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Cedar would skip the erroring forbid and let the request through."""
        cedar = CedarEnforcer("""
            permit(principal, action, resource);
            forbid(principal, action, resource) when { resource.owner == principal };
        """)
        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.authz.cedar"):
            assert not await cedar.enforce("ana", "/articles/7", "GET")
        assert "couldn't evaluate" in caplog.text

    async def test_uids_and_context(self) -> None:
        cedar = CedarEnforcer("""
            permit(principal == Team::"core", action == Action::"deploy", resource)
            when { context.hour < 18 };
        """)
        team, api = {"type": "Team", "id": "core"}, {"type": "Service", "id": "api"}
        assert await cedar.enforce(team, api, "deploy", {"hour": 10})
        assert not await cedar.enforce(team, api, "deploy", {"hour": 20})
        with pytest.raises(TypeError, match="give a name"):
            await cedar.enforce(42, "api", "deploy")

    async def test_whole_entities_with_their_attributes(self) -> None:
        """The user the authentication loaded, the document the route read: attributes for ABAC."""
        cedar = CedarEnforcer("""
            permit(principal, action == Action::"edit", resource)
            when { resource.owner == principal };
            forbid(principal, action, resource)
            when { resource.confidential && principal.contractor };
        """)
        ana = {"uid": {"type": "User", "id": "ana"}, "attrs": {"contractor": False}, "parents": []}
        eva = {"uid": {"type": "User", "id": "eva"}, "attrs": {"contractor": True}, "parents": []}

        def document(owner: str, confidential: bool) -> dict[str, Any]:
            attrs = {
                "owner": {"__entity": {"type": "User", "id": owner}},
                "confidential": confidential,
            }
            return {"uid": {"type": "Document", "id": "7"}, "attrs": attrs, "parents": []}

        assert await cedar.enforce(ana, document("ana", confidential=True), "edit")
        assert not await cedar.enforce(ana, document("eva", confidential=False), "edit")
        assert await cedar.enforce(eva, document("eva", confidential=False), "edit")
        # The forbid wins.
        assert not await cedar.enforce(eva, document("eva", confidential=True), "edit")

    async def test_static_resources_keep_their_attributes(self) -> None:
        """The resource entity is only added when the entities don't have it already."""
        cedar = enforcer()
        assert not await cedar.enforce("ana", "archive", "publish")  # locked, as ROLES says

    async def test_entities_of_may_be_a_coroutine_function(self) -> None:
        """For the roles read through the network."""

        async def from_the_database(subject: str) -> list[dict[str, Any]]:
            return user_entities(subject)

        cedar = CedarEnforcer(POLICIES, entities=ROLES, entities_of=from_the_database)
        assert await cedar.enforce("ana", "articles", "publish")
        assert not await cedar.enforce("bob", "articles", "publish")

    async def test_an_entity_given_twice_is_the_same_each_time(self) -> None:
        """Neither the static entity nor the request's wins: a forbid on `locked` would depend
        on which one was picked."""
        ana = user_entities("ana")[0]
        cedar = CedarEnforcer(POLICIES, entities=ROLES, entities_of=lambda _: [ana])
        assert await cedar.enforce(ana, "articles", "publish")  # entities_of gives it too: equal
        unlocked = {"uid": {"type": "Resource", "id": "archive"}, "attrs": {"id": "archive"}}
        with pytest.raises(ValueError, match='duplicate entity entry `Resource::"archive"`'):
            await cedar.enforce("ana", {**unlocked, "parents": []}, "publish")
        with pytest.raises(ValueError, match='duplicate entity entry `User::"ana"`'):
            await cedar.enforce({**ana, "attrs": {"contractor": True}}, "articles", "publish")


SCHEMA = """
entity Role;
entity User in [Role];
entity Resource { id: String };
action GET, PUT, publish appliesTo { principal: [User], resource: [Resource] };
"""


class TestConfiguration:
    def test_invalid_policies(self) -> None:
        with pytest.raises(ValueError, match="invalid Cedar policies"):
            CedarEnforcer("permit(principal")

    async def test_validated_against_a_schema(self) -> None:
        good = 'permit(principal in Role::"editor", action == Action::"PUT", resource);'
        assert await CedarEnforcer(good, schema=SCHEMA).enforce("ana", "/x", "PUT") is False
        with pytest.raises(ValueError, match="don't fit the schema"):
            CedarEnforcer('permit(principal, action == Action::"delete", resource);', schema=SCHEMA)
        with pytest.raises(ValueError, match="invalid Cedar schema"):
            CedarEnforcer(good, schema="entity {")


def user_header(x_user: Annotated[str | None, Header()] = None) -> str | None:
    return x_user


class TestInAnApp:
    def client(self) -> TestClient:
        authz = FastAPIAuthz(enforcer(), subject=user_header, anonymous="*anonymous")
        app = FastAPI()
        router = APIRouter(dependencies=[Depends(authz)])

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
            user: Annotated[str, Depends(authz.require("articles", "publish"))],
        ) -> dict[str, Any]:
            return {"article": article_id, "by": user}

        return TestClient(app)

    def test_the_policy_decides(self) -> None:
        client = self.client()
        assert client.get("/articles/1", headers={"X-User": "bob"}).status_code == 200
        assert client.put("/articles/1", headers={"X-User": "bob"}).status_code == 403
        assert client.put("/articles/1", headers={"X-User": "ana"}).status_code == 200
        assert client.post("/articles/1/publish", headers={"X-User": "ana"}).json() == {
            "article": 1,
            "by": "ana",
        }
        assert client.post("/articles/1/publish", headers={"X-User": "bob"}).status_code == 403

    def test_anonymous_and_control_characters(self) -> None:
        client = self.client()
        assert client.get("/catalog").json() == ["olive oil"]
        assert client.get("/articles/1").status_code == 401
        assert client.get("/articles/1%0A", headers={"X-User": "bob"}).status_code == 400


class BasicAuth(AuthenticationBackend):
    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, SimpleUser] | None:
        if "Authorization" not in conn.headers:
            return None
        username = base64.b64decode(conn.headers["Authorization"].split()[1]).decode().split(":")[0]
        return AuthCredentials(["authenticated"]), SimpleUser(username)


def test_the_middleware() -> None:
    app = FastAPI()
    app.put("/articles/{article_id}")(lambda article_id: article_id)
    app.add_middleware(AuthzMiddleware, enforcer=enforcer())
    app.add_middleware(AuthenticationMiddleware, backend=BasicAuth())
    client = TestClient(app)
    assert client.put("/articles/1", auth=("ana", "x")).status_code == 200
    assert client.put("/articles/1", auth=("bob", "x")).status_code == 403
    assert client.put("/articles/1").status_code == 401
