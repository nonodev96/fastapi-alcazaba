"""A deadline for every request, so that a hung dependency or a client that trickles its body
doesn't hold a request (and its database connection, its memory...) forever.

    app.add_middleware(RequestTimeoutMiddleware, timeout=30, paths={"/reports": 120})

When a request runs out of time before its response has started, the app is cancelled (its
`finally` blocks and the exit of its `yield` dependencies run) and the client gets a 504. A
response that has started can't become a 504: the app is cancelled all the same and the
connection is closed, so that the client sees a cut response rather than one that looks whole.
"""

import asyncio
import logging
import math
from collections.abc import Mapping

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi_alcazaba.core.asgi import respond, route_path
from fastapi_alcazaba.response_cache.caches import metrics_of

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_GATEWAY_TIMEOUT = 504


def _route(scope: Scope) -> str:
    template = getattr(scope.get("route"), "path_format", None)
    return template if isinstance(template, str) else "unmatched"


def _where(scope: Scope) -> str:
    """The method and route template, for logs; when no route matched, the path quoted with
    repr(): it comes from the client and may hold line breaks."""
    template = getattr(scope.get("route"), "path_format", None)
    path = template if isinstance(template, str) else repr(scope["path"])
    return f"{scope['method']} {path}"


class RequestTimeoutMiddleware:
    """Gives every HTTP request `timeout` seconds, from its arrival until its response has been
    sent; WebSockets and lifespan events go through untouched.

        app.add_middleware(RequestTimeoutMiddleware, timeout=30, paths={"/exports": None})

    - Out of time before the response starts: the app is cancelled and the client gets a 504
      with a small JSON body.
    - Out of time while a response streams: too late for a 504. The app is cancelled and the
      exception (a TimeoutError) raised again, so that the server closes the connection: the
      client sees an incomplete body, never a short one that looks complete. Give streaming
      endpoints (Server-Sent Events, big exports) their own limit in `paths`, or None.
    - Once the response has been sent, background tasks have no deadline.
    - Sync endpoints run in a thread that can't be interrupted: the client gets its 504 on time,
      but the thread runs to its end, after the exit of its `yield` dependencies has run.
      Endpoints that may hit the deadline are better async.

    Both cases are logged as warnings.

    :param timeout: the deadline, in seconds; None for none.
    :param paths: other deadlines for the paths starting with these prefixes (the longest
        matching prefix wins; None for none), matched like the app's routes: without the
        `root_path` a proxy or a mount adds in front.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        timeout: float | None = 30.0,
        paths: Mapping[str, float | None] | None = None,
    ) -> None:
        if timeout is not None and (math.isnan(timeout) or timeout <= 0):
            raise ValueError("timeout must be positive (or None for no deadline)")
        for prefix, seconds in (paths or {}).items():
            if not prefix.startswith("/"):
                raise ValueError(f"path prefixes start with /: {prefix!r}")
            if seconds is not None and not seconds > 0:
                raise ValueError(f"the timeout of {prefix} must be positive (or None)")
        self.app = app
        self.timeout = timeout
        self._paths = sorted((paths or {}).items(), key=lambda item: len(item[0]), reverse=True)

    def timeout_for(self, path: str) -> float | None:
        """The deadline of requests to `path`, in seconds; None when there's none."""
        for prefix, seconds in self._paths:
            base = prefix.rstrip("/")
            if path == base or path.startswith(base + "/"):
                return seconds
        return self.timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        seconds = self.timeout_for(route_path(scope))
        if seconds is None:
            await self.app(scope, receive, send)
            return
        deadline = asyncio.timeout(seconds)
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            started = True
            await send(message)
            # The last message: the response is out, what's left (background tasks) can take
            # its time. After the send, so that a client that won't read can't hold it open.
            if message["type"] != "http.response.start" and not message.get("more_body", False):
                if not deadline.expired():
                    deadline.reschedule(None)

        try:
            async with deadline:
                await self.app(scope, receive, tracked_send)
        except TimeoutError:
            if not deadline.expired():
                raise  # the app's own TimeoutError
            if started:
                logger.warning(
                    "%s timed out after %ss with its response under way: connection closed",
                    _where(scope),
                    seconds,
                )
                metrics_of(HTTPConnection(scope)).count(
                    "request_timeouts", outcome="cut", route=_route(scope)
                )
                raise
        else:
            if not deadline.expired() or started:
                return
            # The app swallowed the cancellation and returned without answering.
        logger.warning("%s timed out after %ss: answered 504", _where(scope), seconds)
        metrics_of(HTTPConnection(scope)).count(
            "request_timeouts", outcome="504", route=_route(scope)
        )
        await respond(scope, send, _GATEWAY_TIMEOUT, "Request timed out")
