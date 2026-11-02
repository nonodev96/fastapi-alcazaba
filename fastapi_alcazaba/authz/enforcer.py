"""Authorization by a policy engine: a policy decides who may do what to which resource (ACL,
RBAC, ABAC...), instead of checks spread over the routes.

    enforcer = casbin.Enforcer("model.conf", "policy.csv")  # pip install "fastapi-alcazaba[casbin]"
    enforcer = CedarEnforcer(policies)  # or Cedar: pip install "fastapi-alcazaba[cedar]"
    authz = FastAPIAuthz(enforcer, subject=current_username)

    @app.get("/articles/{article_id}", dependencies=[Depends(authz)])  # (user, path, method)
    async def article(article_id: int) -> Article: ...

    @app.post("/articles/{article_id}/publish")
    async def publish(user: Annotated[str, Depends(authz.require("articles", "publish"))]) -> None:

`AuthzMiddleware` checks every request instead, with the user of Starlette's
AuthenticationMiddleware.

No engine is imported here: any enforcer with `enforce(*values) -> bool` works, Casbin's
(`Enforcer`, `AsyncEnforcer`, `SyncedEnforcer`, `FastEnforcer`...) as they are, Cedar's through
`fastapi_alcazaba.authz.cedar.CedarEnforcer`. An `enforce` that is a coroutine function is
awaited: an engine that runs as a service of its own (Open Policy Agent, through
`fastapi_alcazaba.authz.opa.OPAEnforcer`) answers through the network without blocking the event
loop, and raises `PolicyUnavailable` when it doesn't answer: a 503. The checks of an engine in
the process run in the event loop, like the changes an AsyncEnforcer makes to its policy: a
request never sees half a change.
"""

import inspect
import logging
import re
import string
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Annotated, Any, NoReturn, Protocol

from fastapi import Depends, HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_503_SERVICE_UNAVAILABLE,
    WS_1008_POLICY_VIOLATION,
    WS_1013_TRY_AGAIN_LATER,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi_alcazaba.core.asgi import respond, route_path
from fastapi_alcazaba.telemetry.metrics import Metrics

if TYPE_CHECKING:
    from fastapi_alcazaba.audit import FastAPIAudit

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The action of a WebSocket, which has no method: a policy for GET doesn't open WebSockets.
WEBSOCKET = "WEBSOCKET"

_DETAIL = {
    HTTP_400_BAD_REQUEST: "Invalid path",
    HTTP_401_UNAUTHORIZED: "Not authenticated",
    HTTP_403_FORBIDDEN: "Forbidden",
    HTTP_503_SERVICE_UNAVAILABLE: "Authorization unavailable",
}
_FORMATTER = string.Formatter()
# Starlette routes "/users/\n" as "/users/" (its patterns end in "$", which matches before a final
# newline) while the policy would see the newline: the two wouldn't agree on what is asked.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PolicyUnavailable(Exception):
    """The policy engine didn't answer: a service that is down, slow or overloaded. The request
    is refused with a 503 (1013, try again later, for a WebSocket), neither allowed nor taken for
    a refusal of the policy. Raise it from an enforcer of your own."""


def _websocket_code(status: int) -> int:
    if status == HTTP_503_SERVICE_UNAVAILABLE:
        return WS_1013_TRY_AGAIN_LATER
    return WS_1008_POLICY_VIOLATION


class Enforcer(Protocol):
    """What FastAPIAuthz needs of a policy engine: Casbin's enforcers have it, `CedarEnforcer`
    gives it to Cedar."""

    def enforce(self, *values: Any) -> bool | Awaitable[bool]:
        """Whether the policy allows the request `values` (sub, obj, act...): a bool, or an
        awaitable of one for an engine that answers through the network."""
        ...


#: Who makes the request: a FastAPI dependency (it can take the request, a token, other
#: dependencies) that returns the subject the policy knows, or None when nobody is authenticated.
Subject = Callable[..., Any]

#: Who an audit event's actor is, from the subject: its ID, or None.
Actor = Callable[[Any], str | int | uuid.UUID | None]


def subject_id(subject: Any) -> str | int | uuid.UUID | None:
    """The ID of a subject, for the audit trail: itself when it's an ID; a Cedar entity's `id`;
    a mapping's `sub`, `id` or `name`; an object's `id` or `identity`."""
    if isinstance(subject, str | int | uuid.UUID) and not isinstance(subject, bool):
        return subject
    if isinstance(subject, Mapping):
        uid = subject.get("uid")
        if isinstance(uid, Mapping):
            return subject_id(uid.get("id"))
        found = next((subject[key] for key in ("sub", "id", "name") if key in subject), None)
    else:
        found = getattr(subject, "id", getattr(subject, "identity", None))
    if found is None or isinstance(found, bool):
        return None
    return found if isinstance(found, str | int | uuid.UUID) else str(found)


def starlette_user(connection: HTTPConnection) -> str | None:
    """The subject of Starlette's AuthenticationMiddleware: its user's `identity` (not the
    `display_name`, which users may choose), None when it isn't authenticated."""
    if "user" not in connection.scope:
        raise RuntimeError(
            "the user comes from Starlette's AuthenticationMiddleware, which isn't installed: "
            "add it, or give FastAPIAuthz a subject="
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


class FastAPIAuthz:
    """FastAPI dependency that lets a request through only when the policy allows it.

        authz = FastAPIAuthz(enforcer, subject=current_username)

        # (subject, path, method): the path the app routes, and the HTTP method.
        router = APIRouter(dependencies=[Depends(authz)])

        # (subject, *values): your own objects and actions; {id} takes the path parameter.
        archive = authz.require("projects/{id}", "archive")
        @app.post("/projects/{id}/archive", dependencies=[Depends(archive)])

    A request without a subject gets a 401 (with `WWW-Authenticate: <challenge>`), unless
    `anonymous` names the subject the policy knows them by; one that the policy refuses, a 403;
    one the engine couldn't answer (`PolicyUnavailable`), a 503. Paths with control characters
    (`%0A`...) get a 400 without asking the policy, as the app could route them elsewhere than
    the policy reads them. On WebSocket routes, the connection is closed with 1008 (policy
    violation) instead, 1013 (try again later) for the 503. Both dependencies return the subject
    (None for anonymous requests let through).

    :param enforcer: a Casbin enforcer, a `CedarEnforcer`, or anything with `enforce` (a plain
        function or a coroutine function), with its policy loaded (`await enforcer.load_policy()`
        in the lifespan for an AsyncEnforcer). `PolicyUnavailable` refuses the request with a
        503; any other exception it raises, with a 500: never the route.
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
    :param audit: the audit trail that records the requests the policy refuses (`authz.denied`:
        the 403s, and the 400s of paths with control characters) and those the engine couldn't
        decide (`authz.unavailable`), with what was asked, the route and the client. Not the
        401s of requests without a subject, which authentication tells, nor `enforce()`'s checks
        in a route, whose answer the route decides what to do with.
    :param actor: who the subject is, in those events: `actor(subject)`, an ID; `subject_id` by
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
        audit: "FastAPIAudit | None" = None,
        actor: Actor | None = None,
    ) -> None:
        if anonymous == "":
            raise ValueError("anonymous is a subject's name, or None")
        self.enforcer = enforcer
        self.subject = subject if subject is not None else starlette_user
        self.anonymous = anonymous
        self.challenge = challenge
        self._metrics = metrics
        self.audit = audit
        self.actor = actor if actor is not None else subject_id
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
        status = await self.path_refusal(subject, connection.scope)
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
            return await self._check(connection, subject, *filled)

        return with_values

    async def enforce(self, subject: Any, *values: Any) -> bool:
        """Whether the policy lets `subject` do `values`, counted in the metrics: for checks
        outside a dependency (a WebSocket message, an object loaded in the route). When the
        engine doesn't answer, an HTTPException 503: the route doesn't go on as if it had."""
        try:
            return await self._decide(subject, *values)
        except PolicyUnavailable:
            status = HTTP_503_SERVICE_UNAVAILABLE
            raise HTTPException(status, _DETAIL[status]) from None

    async def _decide(self, subject: Any, *values: Any) -> bool:
        try:
            decision = self.enforcer.enforce(subject, *values)
            if inspect.isawaitable(decision):
                decision = await decision
        except PolicyUnavailable as exc:
            self._count("unavailable")
            logger.warning("Refused: the policy engine is unavailable: %s", exc)
            raise
        allowed = bool(decision)
        self._count("allowed" if allowed else "denied")
        if not allowed:
            logger.debug("The policy refused %r", (subject, *values))
        return allowed

    async def refusal(
        self,
        subject: Any,
        *values: Any,
        connection: HTTPConnection | None = None,
    ) -> int | None:
        """None when the policy lets `subject` (None for nobody) do `values`; else the status to
        refuse it with: 401 without a subject, 403 with one, 503 when the engine didn't answer.
        With the `connection` it's for, the refusals go to the audit trail."""
        if subject is None and self.anonymous is None:
            self._count("unauthenticated")
            return HTTP_401_UNAUTHORIZED
        try:
            allowed = await self._decide(self.anonymous if subject is None else subject, *values)
        except PolicyUnavailable:
            await self._audit(connection, HTTP_503_SERVICE_UNAVAILABLE, subject, values)
            return HTTP_503_SERVICE_UNAVAILABLE
        if allowed:
            return None
        if subject is None:
            return HTTP_401_UNAUTHORIZED
        await self._audit(connection, HTTP_403_FORBIDDEN, subject, values)
        return HTTP_403_FORBIDDEN

    async def path_refusal(self, subject: Any, scope: Scope) -> int | None:
        """`refusal` of (subject, path, method): the path the app routes and the HTTP method
        (`WEBSOCKET` for WebSockets; GET for HEAD, which runs the GET route). A 400 for paths with
        control characters, without asking the policy: the app could route them elsewhere than
        the policy reads them."""
        path = route_path(scope)
        method = scope.get("method", WEBSOCKET)
        values = (path, "GET" if method == "HEAD" else method)
        connection = HTTPConnection(scope)
        if _CONTROL.search(path):
            self._count("denied")
            await self._audit(connection, HTTP_400_BAD_REQUEST, subject, values)
            return HTTP_400_BAD_REQUEST
        return await self.refusal(subject, *values, connection=connection)

    async def _check(self, connection: HTTPConnection, subject: Any, *values: Any) -> Any:
        status = await self.refusal(subject, *values, connection=connection)
        if status is None:
            return subject
        self._refuse(connection, status)

    def _refuse(self, connection: HTTPConnection, status: int) -> NoReturn:
        if connection.scope["type"] == "websocket":
            raise WebSocketException(_websocket_code(status), _DETAIL[status])
        headers = {"WWW-Authenticate": self.challenge} if status == HTTP_401_UNAUTHORIZED else None
        raise HTTPException(status, _DETAIL[status], headers=headers)

    async def _audit(
        self,
        connection: HTTPConnection | None,
        status: int,
        subject: Any,
        values: tuple[Any, ...],
    ) -> None:
        if self.audit is None or connection is None:
            return
        unavailable = status == HTTP_503_SERVICE_UNAVAILABLE
        await self.audit.log(
            "authz.unavailable" if unavailable else "authz.denied",
            outcome="failure" if unavailable else "denied",
            actor=self.actor(subject) if subject is not None else None,
            resource=values[0] if values and isinstance(values[0], str) else None,
            reason=_DETAIL[status],
            details={"request": list(values), "status": status},
            request=connection,
        )

    def _count(self, outcome: str) -> None:
        if self._metrics is not None:
            metrics = self._metrics
        else:
            from fastapi_alcazaba.response_cache.caches import FastAPICache

            metrics = FastAPICache.get_metrics()
        metrics.count("authz", outcome=outcome)


class AuthzMiddleware:
    """Checks every request against the policy, with the user of Starlette's
    AuthenticationMiddleware. Add it before AuthenticationMiddleware, so that it runs after it:

        app.add_middleware(AuthzMiddleware, enforcer=enforcer)
        app.add_middleware(AuthenticationMiddleware, backend=BasicAuth())

    Each request is checked as (user, path, method): the user's `identity` (`anonymous` when it
    isn't authenticated), the path the app routes (without the `root_path` a proxy or a mount
    adds in front) and the method, GET for HEAD and `WEBSOCKET` for WebSockets. Refused requests
    get a 403, or a 401 (with `WWW-Authenticate: <challenge>`) when they have no user, and a 503
    when the engine didn't answer (`PolicyUnavailable`); WebSockets are closed with 1008 (policy
    violation), or 1013 (try again later). Paths with control characters (`%0A`...) get a 400
    without asking the policy, as the app could route them elsewhere than the policy reads them.
    Every request is checked, OPTIONS too: add CORSMiddleware after this one (outside it), and it
    answers the preflights before they get here.

    :param enforcer: a Casbin enforcer, a `CedarEnforcer`, or anything with `enforce` (a plain
        function or a coroutine function), with its policy loaded.
    :param anonymous: the subject of requests without a user, a name no user can have
        (`*anonymous`); None refuses them without asking the policy.
    :param challenge: the `WWW-Authenticate` of the 401s.
    :param metrics: where to count the decisions (`fastapi_alcazaba.authz`); FastAPICache's by
        default.
    :param audit: the audit trail that records the refusals, as FastAPIAuthz's `audit` says.
    :param actor: who the subject is, in those events; `subject_id` by default.
    """

    def __init__(
        self,
        app: ASGIApp,
        enforcer: Enforcer,
        *,
        anonymous: str | None = None,
        challenge: str = "Bearer",
        metrics: Metrics | None = None,
        audit: "FastAPIAudit | None" = None,
        actor: Actor | None = None,
    ) -> None:
        self.app = app
        self.authz = FastAPIAuthz(
            enforcer,
            anonymous=anonymous,
            challenge=challenge,
            metrics=metrics,
            audit=audit,
            actor=actor,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        subject = starlette_user(HTTPConnection(scope))
        status = await self.authz.path_refusal(subject, scope)
        if status is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            if (await receive())["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": _websocket_code(status)})
        else:
            headers = (
                {"WWW-Authenticate": self.authz.challenge}
                if status == HTTP_401_UNAUTHORIZED
                else None
            )
            await respond(scope, send, status, _DETAIL[status], headers)
