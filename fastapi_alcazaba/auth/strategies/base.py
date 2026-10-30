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
"""

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from fastapi import HTTPException, Security, WebSocketException
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.status import HTTP_401_UNAUTHORIZED, WS_1008_POLICY_VIOLATION

from fastapi_alcazaba.telemetry.metrics import Metrics

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class AuthenticationFailed(Exception):
    """Raised by a strategy whose credentials are in the request but wrong: an expired token, a
    forged one, an OAuth2 login the provider refused. The message is logged, never sent."""


class Strategy(ABC):
    """One way to tell who makes a request. Subclasses implement `authenticate`.

    :param name: what `FastAPIAuth.authenticate` calls it by.
    """

    #: The `WWW-Authenticate` challenge of a 401 when no strategy found a user ("Bearer"...).
    challenge: str | None = None

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

    :param strategies: the strategies, by their names; more with `use`.
    :param metrics: where to count the outcomes (`fastapi_alcazaba.auth`); FastAPICache's by
        default.
    """

    def __init__(self, *strategies: Strategy, metrics: Metrics | None = None) -> None:
        self.strategies: dict[str, Strategy] = {}
        self._metrics = metrics
        for strategy in strategies:
            self.use(strategy)

    def use(self, strategy: S) -> S:
        """Add a strategy, under its name; returns it, with its own type (`github.redirect()`)."""
        if strategy.name in self.strategies:
            raise ValueError(f"there's a strategy called {strategy.name!r} already")
        self.strategies[strategy.name] = strategy
        return strategy

    def authenticate(self, *names: str, optional: bool = False) -> Callable[..., Awaitable[Any]]:
        """A dependency that returns the user of the first of these strategies that finds one
        (all of them, in the order they were added, without names). Without one, a 401 with
        their challenges (a WebSocket is closed with 1008), or None when `optional`."""
        strategies = [self._strategy(name) for name in names] or list(self.strategies.values())
        if not strategies:
            raise ValueError("there are no strategies: add them with use()")

        async def dependency(connection: HTTPConnection, **_: Any) -> Any:
            user = await self.user(connection, *[strategy.name for strategy in strategies])
            if user is not None or optional:
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
            )
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
        for strategy in strategies:
            try:
                user = await strategy.authenticate(connection)
            except AuthenticationFailed as exc:
                logger.info("Authentication by %s failed: %s", strategy.name, exc)
                self._count("failure", strategy.name)
                continue
            if user is not None:
                self._count("success", strategy.name)
                return user
        self._count("unauthenticated", ",".join(strategy.name for strategy in strategies))
        return None

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
