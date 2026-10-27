"""What Alcazaba's ASGI middlewares share: the path the app routes, and their own answers."""

import json
import re
from collections.abc import Mapping

from starlette.requests import Request
from starlette.types import Message, Scope, Send

#: A header name, or a token in a header value (RFC 9110).
TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
#: Where in the request's state the rate limiters leave their headers, for the error responses
#: that FastAPIProblemDetails builds (FastAPI doesn't give them the dependencies' headers).
RATE_LIMIT_HEADERS = "fastapi_alcazaba_rate_limit_headers"


def route_path(scope: Scope) -> str:
    """The path the app routes, without the `root_path` a proxy or a mount adds in front."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        if path == root_path:
            return "/"
        if path[len(root_path)] == "/":
            return path[len(root_path) :]
    return path


async def respond(
    scope: Scope,
    send: Send,
    status: int,
    detail: str,
    headers: Mapping[str, str] | None = None,
) -> None:
    """A middleware's own answer: a problem when the app has FastAPIProblemDetails installed (so
    that every error of the app looks the same), else FastAPI's `{"detail": ...}`."""
    problems = getattr(getattr(scope.get("app"), "state", None), "fastapi_alcazaba_problems", None)
    if problems is not None:
        from fastapi_alcazaba.observability.problems import Problem

        # A Problem: its detail is sent, also with a 5xx, as it's written for the client.
        response = problems.response(Request(scope), Problem(status, detail, headers=headers))
        await response(scope, _disconnected, send)
        return
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))]
    raw += [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


async def _disconnected() -> Message:
    """The receive of an answer that reads nothing."""
    return {"type": "http.disconnect"}
