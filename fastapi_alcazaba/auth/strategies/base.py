"""Authentication by strategies: each strategy knows one way to tell who makes a request (a JWT,
a session, an OAuth2 login...), and a dependency tries them in order.

    auth = FastAPIAuth()
    auth.use(JWTStrategy(key=settings.jwt_secret, verify=load_user))
    auth.use(SessionStrategy(load_user))

    @app.get("/me")
    async def me(user: Annotated[User, Depends(auth.authenticate("jwt", "session"))]) -> User:
        return user

A strategy returns the user, None when the request doesn't carry its credentials, or raises
AuthenticationFailed when it carries them but they're wrong. The first user found is the
request's; when none is, a 401 with the challenges of the strategies tried.

Routes that need scopes ask for them the FastAPI way, which the OpenAPI schema shows:

    @app.post("/orders", dependencies=[Security(auth.authenticate("jwt"), scopes=["orders:write"])])

The strategy that found the user grants the scopes of its credentials (a JWT's `scope`); one
that's missing is a 403, `WWW-Authenticate: Bearer error="insufficient_scope"`.
"""

import inspect
import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import TYPE_CHECKING, Annotated, Any, TypeVar
from urllib.parse import urlsplit

from fastapi import HTTPException, Security, WebSocketException
from fastapi.security import SecurityScopes
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN, WS_1008_POLICY_VIOLATION

from fastapi_alcazaba.telemetry.metrics import Metrics

if TYPE_CHECKING:
    from fastapi_alcazaba.audit import FastAPIAudit

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Where in the request's state the scopes granted, and the users found, are kept.
_GRANTED = "fastapi_alcazaba_scopes"
_FOUND = "fastapi_alcazaba_auth"
#: RFC 6749's scope-token: printable ASCII but space, " and \\.
_SCOPE = re.compile(r"[!#-\[\]-~]+")

#: Who an audit event's actor is, from the user a strategy found: its ID, or None.
Actor = Callable[[Any], str | int | uuid.UUID | None]


class AuthenticationFailed(Exception):
    """Raised by a strategy whose credentials are in the request but wrong: an expired token, a
    forged one, an OAuth2 login the provider refused. The message is logged, never sent."""


class Strategy(ABC):
    """One way to tell who makes a request. Subclasses implement `authenticate`.

    :param name: what `FastAPIAuth.authenticate` calls it by.
    """

    #: The `WWW-Authenticate` challenge of a 401 when no strategy found a user ("Bearer"...).
    challenge: str | None = None
    #: Whether finding a user is a login (an OAuth2 or SAML callback), which the audit trail
    #: records, rather than a request that carries its credentials.
    logs_in: bool = False

    def __init__(self, name: str) -> None:
        if not name:
            raise ValueError("a strategy needs a name")
        self.name = name

    @abstractmethod
    async def authenticate(self, connection: HTTPConnection) -> Any:
        """The user who makes the request (a Request or a WebSocket); None when it doesn't carry
        this strategy's credentials. Raises AuthenticationFailed when they're there but wrong."""

    def openapi_scheme(self) -> Callable[..., Any] | None:
        """The FastAPI security scheme that the OpenAPI schema shows for it, for the docs'
        Authorize button (`HTTPBearer(auto_error=False)`...); None for none."""
        return None

    def grant(self, connection: HTTPConnection, scopes: str | Collection[str]) -> None:
        """Record the scopes that the request's credentials grant (a token's `scope`, a key's),
        for the routes that need some: call it in `authenticate`, with the user found."""
        granted = frozenset(scopes.split() if isinstance(scopes, str) else scopes)
        if not all(isinstance(scope, str) and _SCOPE.fullmatch(scope) for scope in granted):
            raise AuthenticationFailed("the credentials' scopes aren't scope tokens")
        connection.scope.setdefault("state", {})[_GRANTED] = granted


def granted_scopes(connection: HTTPConnection) -> frozenset[str] | None:
    """The scopes the credentials of the request's user grant; None when the strategy that found
    it grants none (a session, a login)."""
    granted = connection.scope.get("state", {}).get(_GRANTED)
    return granted if isinstance(granted, frozenset) else None


def _actor(user: Any) -> str | int | uuid.UUID | None:
    """The ID of a user, for the audit trail: itself when it's an ID; its `sub` or `id` (a
    mapping's, or an object's attribute)."""
    if isinstance(user, str | int | uuid.UUID) and not isinstance(user, bool):
        return user
    if isinstance(user, Mapping):
        found = user.get("sub", user.get("id"))
    else:
        found = getattr(user, "id", None)
    if found is None or isinstance(found, bool):
        return None
    return found if isinstance(found, str | int | uuid.UUID) else str(found)


async def call(function: Callable[..., Any], *args: Any) -> Any:
    """`function(*args)`, awaited when it's async; a plain function runs in a thread, as FastAPI
    runs sync dependencies."""
    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)  # noqa: B004  # objects with an async __call__
    ):
        return await function(*args)
    result = await run_in_threadpool(function, *args)
    return await result if inspect.isawaitable(result) else result


S = TypeVar("S", bound=Strategy)


class FastAPIAuth:
    """The strategies of an app, and the dependencies that authenticate with them.

        auth = FastAPIAuth(JWTStrategy(...), SessionStrategy(...))

        @app.get("/me")
        async def me(user: Annotated[User, Depends(auth.authenticate("jwt", "session"))]): ...

    A request is authenticated once: the dependencies of a route that ask for the same
    strategies get the user the first one found (an API key isn't verified twice).

    :param strategies: the strategies, by their names; more with `use`.
    :param metrics: where to count the outcomes (`fastapi_alcazaba.auth`); FastAPICache's by
        default.
    :param audit: the audit trail that records the failed authentications (`auth.failure`),
        the logins of OAuth2, OpenID Connect and SAML (`auth.login`) and the requests without a
        scope they need (`auth.scope`), with the request's route and client; not every request
        authenticated.
    :param actor: who the user a strategy found is, in those events: `actor(user)`, an ID. By
        default the user itself when it's an ID, else its `sub` or `id`.
    """

    def __init__(
        self,
        *strategies: Strategy,
        metrics: Metrics | None = None,
        audit: "FastAPIAudit | None" = None,
        actor: Actor | None = None,
    ) -> None:
        self.strategies: dict[str, Strategy] = {}
        self._metrics = metrics
        self.audit = audit
        self.actor = actor if actor is not None else _actor
        for strategy in strategies:
            self.use(strategy)

    def use(self, strategy: S) -> S:
        """Add a strategy, under its name; returns it, with its own type (`github.redirect()`)."""
        if strategy.name in self.strategies:
            raise ValueError(f"there's a strategy called {strategy.name!r} already")
        self.strategies[strategy.name] = strategy
        return strategy

    def authenticate(
        self,
        *names: str,
        optional: bool = False,
        scopes: Collection[str] = (),
    ) -> Callable[..., Awaitable[Any]]:
        """A dependency that returns the user of the first of these strategies that finds one
        (all of them, in the order they were added, without names). Without one, a 401 with
        their challenges (a WebSocket is closed with 1008), or None when `optional`.

        The user's credentials must grant `scopes`, and those of `Security(dependency,
        scopes=[...])` where it's used (FastAPI's, which the OpenAPI schema shows); one missing
        is a 403 with `WWW-Authenticate: Bearer error="insufficient_scope"`."""
        strategies = [self._strategy(name) for name in names] or list(self.strategies.values())
        if not strategies:
            raise ValueError("there are no strategies: add them with use()")
        if isinstance(scopes, str):
            raise TypeError("scopes is a list of scopes, not a string")
        static = frozenset(scopes)
        if not all(_SCOPE.fullmatch(scope) for scope in static):
            raise ValueError("scopes are printable ASCII without spaces")

        async def dependency(
            connection: HTTPConnection,
            security_scopes: SecurityScopes,
            **_: Any,
        ) -> Any:
            user, found = await self._found(connection, strategies)
            if user is not None:
                needed = static | frozenset(security_scopes.scopes)
                if needed:
                    await self._check_scopes(connection, user, found, needed)
                return user
            if optional:
                return user
            if connection.scope["type"] == "websocket":
                raise WebSocketException(WS_1008_POLICY_VIOLATION, "Not authenticated")
            challenges = [strategy.challenge for strategy in strategies if strategy.challenge]
            headers = {"WWW-Authenticate": ", ".join(challenges)} if challenges else None
            raise HTTPException(HTTP_401_UNAUTHORIZED, "Not authenticated", headers=headers)

        # Each strategy's scheme in the signature, for FastAPI to put in the OpenAPI schema.
        parameters = [
            inspect.Parameter(
                "connection", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=HTTPConnection
            ),
            inspect.Parameter(
                "security_scopes",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=SecurityScopes,
            ),
        ]
        for index, strategy in enumerate(strategies):
            scheme = strategy.openapi_scheme()
            if scheme is not None:
                parameters.append(
                    inspect.Parameter(
                        f"scheme_{index}",
                        inspect.Parameter.KEYWORD_ONLY,
                        default=None,
                        annotation=Annotated[Any, Security(scheme)],
                    )
                )
        dependency.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            parameters, return_annotation=Any
        )
        return dependency

    async def user(self, connection: HTTPConnection, *names: str) -> Any:
        """The user of the first of these strategies (all of them without names) that finds one;
        None when none does. For connections that don't go through a dependency."""
        strategies = [self._strategy(name) for name in names] or list(self.strategies.values())
        return (await self._found(connection, strategies))[0]

    async def _found(
        self,
        connection: HTTPConnection,
        strategies: list[Strategy],
    ) -> tuple[Any, Strategy | None]:
        """The user and the strategy that found it, once per request and strategies: what a
        second dependency asks for is what the first one found."""
        state = connection.scope.setdefault("state", {})
        found: dict[tuple[str, ...], tuple[Any, Strategy | None, frozenset[str] | None]]
        found = state.setdefault(_FOUND, {})
        key = tuple(strategy.name for strategy in strategies)
        if key in found:
            user, strategy, granted = found[key]
            state[_GRANTED] = granted
            return user, strategy
        result: tuple[Any, Strategy | None] = (None, None)
        for strategy in strategies:
            state.pop(_GRANTED, None)  # only the strategy that finds the user grants scopes
            try:
                user = await strategy.authenticate(connection)
            except AuthenticationFailed as exc:
                logger.info("Authentication by %s failed: %s", strategy.name, exc)
                self._count("failure", strategy.name)
                await self._audit("auth.failure", "failure", connection, None, strategy)
                continue
            if user is not None:
                self._count("success", strategy.name)
                if strategy.logs_in:
                    await self._audit("auth.login", "success", connection, user, strategy)
                result = (user, strategy)
                break
        else:
            state.pop(_GRANTED, None)
            self._count("unauthenticated", ",".join(strategy.name for strategy in strategies))
        found[key] = (*result, state.get(_GRANTED))
        return result

    async def _check_scopes(
        self,
        connection: HTTPConnection,
        user: Any,
        strategy: Strategy | None,
        needed: frozenset[str],
    ) -> None:
        missing = needed - (granted_scopes(connection) or frozenset())
        if not missing:
            return
        name = strategy.name if strategy is not None else ""
        self._count("insufficient_scope", name)
        await self._audit(
            "auth.scope", "denied", connection, user, strategy, {"missing": sorted(missing)}
        )
        if connection.scope["type"] == "websocket":
            raise WebSocketException(WS_1008_POLICY_VIOLATION, "Insufficient scope")
        headers = None
        if strategy is not None and (strategy.challenge or "").startswith("Bearer"):
            scope = " ".join(sorted(needed))
            headers = {"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'}
        raise HTTPException(HTTP_403_FORBIDDEN, "Insufficient scope", headers=headers)

    async def _audit(
        self,
        action: str,
        outcome: Any,
        connection: HTTPConnection,
        user: Any,
        strategy: Strategy | None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if self.audit is None:
            return
        await self.audit.log(
            action,
            outcome=outcome,
            actor=self.actor(user) if user is not None else None,
            details={"strategy": strategy.name if strategy else None, **(details or {})},
            request=connection,
        )

    def _strategy(self, name: str) -> Strategy:
        try:
            return self.strategies[name]
        except KeyError:
            raise KeyError(f"no strategy called {name!r}: add it with use() first") from None

    def _count(self, outcome: str, strategy: str) -> None:
        if self._metrics is not None:
            metrics = self._metrics
        else:
            from fastapi_alcazaba.response_cache.caches import FastAPICache

            metrics = FastAPICache.get_metrics()
        metrics.count("auth", outcome=outcome, strategy=strategy)


def _secure(url: str) -> bool:
    """HTTPS, or plain HTTP to this machine (a provider in development)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
