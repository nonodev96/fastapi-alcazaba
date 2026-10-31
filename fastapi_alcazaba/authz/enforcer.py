"""Authorization with Casbin: a policy decides who may do what to which resource (ACL, RBAC, RBAC
with domains, ABAC...), instead of checks spread over the routes.

    enforcer = casbin.Enforcer("model.conf", "policy.csv")  # pip install "fastapi-alcazaba[casbin]"
    authz = FastAPICasbin(enforcer, subject=current_username)

    @app.get("/articles/{article_id}", dependencies=[Depends(authz)])  # (user, path, method)
    async def article(article_id: int) -> Article: ...

    @app.post("/articles/{article_id}/publish")
    async def publish(user: Annotated[str, Depends(authz.require("articles", "publish"))]) -> None:

`CasbinMiddleware` checks every request instead, with the user of Starlette's
AuthenticationMiddleware.

Casbin itself isn't imported here: any enforcer with casbin's `enforce(*values) -> bool` works
(`Enforcer`, `AsyncEnforcer`, `SyncedEnforcer`, `FastEnforcer`...). The checks run in the event
loop, like the changes an AsyncEnforcer makes to its policy: a request never sees half a change.
"""

import inspect
import logging
import re
import string
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, NoReturn, Protocol

from fastapi import Depends, HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    WS_1008_POLICY_VIOLATION,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi_alcazaba.core.asgi import respond, route_path
from fastapi_alcazaba.telemetry.metrics import Metrics

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The action of a WebSocket, which has no method: a policy for GET doesn't open WebSockets.
WEBSOCKET = "WEBSOCKET"

_DETAIL = {
    HTTP_400_BAD_REQUEST: "Invalid path",
    HTTP_401_UNAUTHORIZED: "Not authenticated",
    HTTP_403_FORBIDDEN: "Forbidden",
}
_FORMATTER = string.Formatter()
# Starlette routes "/users/\n" as "/users/" (its patterns end in "$", which matches before a final
# newline) while the policy would see the newline: the two wouldn't agree on what is asked.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Enforcer(Protocol):
    """What FastAPICasbin needs of a Casbin enforcer."""

    def enforce(self, *values: Any) -> bool:
        """Whether the policy allows the request `values` (sub, obj, act...)."""
        ...


#: Who makes the request: a FastAPI dependency (it can take the request, a token, other
#: dependencies) that returns the subject the policy knows, or None when nobody is authenticated.
Subject = Callable[..., Any]


def starlette_user(connection: HTTPConnection) -> str | None:
    """The subject of Starlette's AuthenticationMiddleware: its user's `identity` (not the
    `display_name`, which users may choose), None when it isn't authenticated."""
    if "user" not in connection.scope:
        raise RuntimeError(
            "the user comes from Starlette's AuthenticationMiddleware, which isn't installed: "
            "add it, or give FastAPICasbin a subject="
        )
    user = connection.scope["user"]
    return user.identity if user.is_authenticated else None


def _template(value: str) -> tuple[str, tuple[str, ...]]:
    """`value` and the path parameters it names in braces; constants come back unescaped."""
    if not isinstance(value, str):
        raise TypeError(f"the values are strings, not {type(value).__name__}")
    names = []
    for _, name, spec, conversion in _FORMATTER.parse(value):
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise ValueError(
                f"{value!r}: only the {{name}} of a path parameter goes in braces "
                "({{ and }} for the braces themselves)"
            )
        names.append(name)
    return (value, tuple(names)) if names else (value.format(), ())


def _fill(value: str, names: tuple[str, ...], params: Mapping[str, Any]) -> str:
    if not names:
        return value
    for name in names:
        if name not in params:
            raise RuntimeError(f"{value!r} names {name!r}, which isn't a path parameter here")
    return value.format_map(params)


class FastAPICasbin:
    """FastAPI dependency that lets a request through only when the Casbin policy allows it.

        authz = FastAPICasbin(enforcer, subject=current_username)

        # (subject, path, method): the path the app routes, and the HTTP method.
        router = APIRouter(dependencies=[Depends(authz)])

        # (subject, *values): your own objects and actions; {id} takes the path parameter.
        archive = authz.require("projects/{id}", "archive")
        @app.post("/projects/{id}/archive", dependencies=[Depends(archive)])

    A request without a subject gets a 401 (with `WWW-Authenticate: <challenge>`), unless
    `anonymous` names the subject the policy knows them by; one that the policy refuses, a 403.
    Paths with control characters (`%0A`...) get a 400 without asking the policy, as the app could
    route them elsewhere than the policy reads them. On WebSocket routes, the connection is closed
    with 1008 (policy violation) instead. Both dependencies return the subject (None for anonymous
    requests let through).

    :param enforcer: a Casbin enforcer, or anything with its `enforce`, with its policy loaded
        (`await enforcer.load_policy()` in the lifespan for an AsyncEnforcer).
    :param subject: a dependency returning who makes the request (a user name, an id, an object
        for ABAC matchers), or None when nobody is authenticated. By default the user of
        Starlette's AuthenticationMiddleware (`starlette_user`).
    :param anonymous: the subject of requests without one, for policies that open some routes to
        everybody (`p, anonymous, /catalog, GET`); None refuses them without asking the policy.
        A name no user can have (`*anonymous`), or a user who takes it gets its rules, and every
        anonymous request that user's.
    :param challenge: the `WWW-Authenticate` of the 401s: the scheme your users log in with.
    :param metrics: where to count the decisions (`fastapi_alcazaba.authz`); FastAPICache's by
        default.
    """

    def __init__(
        self,
        enforcer: Enforcer,
        *,
        subject: Subject | None = None,
        anonymous: str | None = None,
        challenge: str = "Bearer",
        metrics: Metrics | None = None,
    ) -> None:
        if anonymous == "":
            raise ValueError("anonymous is a subject's name, or None")
        self.enforcer = enforcer
        self.subject = subject if subject is not None else starlette_user
        self.anonymous = anonymous
        self.challenge = challenge
        self._metrics = metrics
        # What FastAPI reads to know what the dependency needs: the subject's own dependency.
        self.__signature__ = inspect.Signature(
            [
                inspect.Parameter(
                    "connection", inspect.Parameter.KEYWORD_ONLY, annotation=HTTPConnection
                ),
                inspect.Parameter(
                    "subject",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=Annotated[Any, Depends(self.subject)],
                ),
            ],
            return_annotation=Any,
        )

    async def __call__(self, *, connection: HTTPConnection, subject: Any) -> Any:
        status = self.path_refusal(subject, connection.scope)
        if status is None:
            return subject
        self._refuse(connection, status)

    def require(self, *values: str) -> Callable[..., Awaitable[Any]]:
        """A dependency that checks (subject, *values) instead of the path and method:
        `Depends(authz.require("articles", "publish"))`. `{name}` in a value takes the path
        parameter `name` (`"projects/{project_id}"`); `{{` and `}}` are braces."""
        if not values:
            raise ValueError("give the values that follow the subject in the policy's requests")
        templates = [_template(value) for value in values]

        async def with_values(
            connection: HTTPConnection,
            subject: Annotated[Any, Depends(self.subject)],
        ) -> Any:
            params = connection.path_params
            filled = [_fill(value, names, params) for value, names in templates]
            return self._check(connection, subject, *filled)

        return with_values

    def enforce(self, subject: Any, *values: Any) -> bool:
        """Whether the policy lets `subject` do `values`, counted in the metrics: for checks
        outside a dependency (a WebSocket message, an object loaded in the route)."""
        allowed = bool(self.enforcer.enforce(subject, *values))
        self._count("allowed" if allowed else "denied")
        if not allowed:
            logger.debug("Casbin refused %r", (subject, *values))
        return allowed

    def refusal(self, subject: Any, *values: Any) -> int | None:
        """None when the policy lets `subject` (None for nobody) do `values`; else the status to
        refuse it with: 401 without a subject, 403 with one."""
        if subject is None:
            if self.anonymous is None:
                self._count("unauthenticated")
                return HTTP_401_UNAUTHORIZED
            return None if self.enforce(self.anonymous, *values) else HTTP_401_UNAUTHORIZED
        return None if self.enforce(subject, *values) else HTTP_403_FORBIDDEN

    def path_refusal(self, subject: Any, scope: Scope) -> int | None:
        """`refusal` of (subject, path, method): the path the app routes and the HTTP method
        (`WEBSOCKET` for WebSockets; GET for HEAD, which runs the GET route). A 400 for paths with
        control characters, without asking the policy: the app could route them elsewhere than
        the policy reads them."""
        path = route_path(scope)
        if _CONTROL.search(path):
            self._count("denied")
            return HTTP_400_BAD_REQUEST
        method = scope.get("method", WEBSOCKET)
        return self.refusal(subject, path, "GET" if method == "HEAD" else method)

    def _check(self, connection: HTTPConnection, subject: Any, *values: Any) -> Any:
        status = self.refusal(subject, *values)
        if status is None:
            return subject
        self._refuse(connection, status)

    def _refuse(self, connection: HTTPConnection, status: int) -> NoReturn:
        if connection.scope["type"] == "websocket":
            raise WebSocketException(WS_1008_POLICY_VIOLATION, _DETAIL[status])
        headers = {"WWW-Authenticate": self.challenge} if status == HTTP_401_UNAUTHORIZED else None
        raise HTTPException(status, _DETAIL[status], headers=headers)

    def _count(self, outcome: str) -> None:
        if self._metrics is not None:
            metrics = self._metrics
        else:
            from fastapi_alcazaba.response_cache.caches import FastAPICache

            metrics = FastAPICache.get_metrics()
        metrics.count("authz", outcome=outcome)


class CasbinMiddleware:
    """Checks every request against the Casbin policy, with the user of Starlette's
    AuthenticationMiddleware. Add it before AuthenticationMiddleware, so that it runs after it:

        app.add_middleware(CasbinMiddleware, enforcer=enforcer)
        app.add_middleware(AuthenticationMiddleware, backend=BasicAuth())

    Each request is checked as (user, path, method): the user's `identity` (`anonymous` when it
    isn't authenticated), the path the app routes (without the `root_path` a proxy or a mount
    adds in front) and the method, GET for HEAD and `WEBSOCKET` for WebSockets. Refused requests
    get a 403, or a 401 (with `WWW-Authenticate: <challenge>`) when they have no user; WebSockets
    are closed with 1008 (policy violation). Paths with control characters (`%0A`...) get a 400
    without asking the policy, as the app could route them elsewhere than the policy reads them.
    Every request is checked, OPTIONS too: add CORSMiddleware after this one (outside it), and it
    answers the preflights before they get here.

    :param enforcer: a Casbin enforcer, or anything with its `enforce`, with its policy loaded.
    :param anonymous: the subject of requests without a user, a name no user can have
        (`*anonymous`); None refuses them without asking the policy.
    :param challenge: the `WWW-Authenticate` of the 401s.
    :param metrics: where to count the decisions (`fastapi_alcazaba.authz`); FastAPICache's by
        default.
    """

    def __init__(
        self,
        app: ASGIApp,
        enforcer: Enforcer,
        *,
        anonymous: str | None = None,
        challenge: str = "Bearer",
        metrics: Metrics | None = None,
    ) -> None:
        self.app = app
        self.authz = FastAPICasbin(
            enforcer, anonymous=anonymous, challenge=challenge, metrics=metrics
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        subject = starlette_user(HTTPConnection(scope))
        status = self.authz.path_refusal(subject, scope)
        if status is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            if (await receive())["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": WS_1008_POLICY_VIOLATION})
        else:
            headers = (
                {"WWW-Authenticate": self.authz.challenge}
                if status == HTTP_401_UNAUTHORIZED
                else None
            )
            await respond(scope, send, status, _DETAIL[status], headers)
