"""Step-up: the routes that need a second factor given recently, in this session, by the user
who makes the request. `FastAPITOTP` and `FastAPIWebAuthn` mark the session when they accept
one; their `require_recent` checks it.

    totp.mark(request, user.id)  # after verify() accepted the user's code

    @app.delete("/account", dependencies=[Depends(require_recent(current_user_id, max_age=300))])
    async def delete_account() -> None: ...
"""

import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

# Where in the session the last second factor is kept: whose, when and which.
_SESSION_KEY = "fastapi_alcazaba_mfa"

#: A user's ID.
UserId = str | int | uuid.UUID


def mark(request: HTTPConnection, user_id: UserId, method: str) -> None:
    """Note in the session that `user_id` just gave a second factor (`method`: "totp",
    "passkey"...), and give the session a new id (with ServerSessionMiddleware): it holds more
    privileges now."""
    from fastapi_alcazaba.security.sessions import ServerSession, rotate_session

    session = _session(request)
    session[_SESSION_KEY] = {"user": str(user_id), "at": time.time(), "method": method}
    if isinstance(session, ServerSession):
        rotate_session(request)


def verified_at(request: HTTPConnection, user_id: UserId) -> float | None:
    """When `user_id` last gave a second factor in this session (Unix time); None when they
    didn't, or someone else did."""
    found = _session(request).get(_SESSION_KEY)
    if not isinstance(found, dict) or found.get("user") != str(user_id):
        return None
    at = found.get("at")
    return float(at) if isinstance(at, int | float) and not isinstance(at, bool) else None


def require_recent(user_id: Callable[..., Any], max_age: int = 300) -> Callable[..., Any]:
    """A dependency that lets through the users who gave a second factor in the last `max_age`
    seconds in this session, and returns their ID; the others get a 403 ("Second factor
    required"), to ask for one and `mark` the session, and requests without a user a 401.
    `user_id` is a dependency that returns the request's user ID, or None."""
    if max_age < 1:
        raise ValueError("max_age is 1 second or more")

    async def recent(
        request: HTTPConnection,
        current: Annotated[Any, Depends(user_id)],
    ) -> Any:
        if current is None:
            raise HTTPException(HTTP_401_UNAUTHORIZED, "Not authenticated")
        at = verified_at(request, current)
        if at is None or not 0 <= time.time() - at <= max_age:
            raise HTTPException(HTTP_403_FORBIDDEN, "Second factor required")
        return current

    return recent


def _session(request: HTTPConnection) -> dict[str, Any]:
    session: dict[str, Any] | None = request.scope.get("session")
    if session is None:
        raise RuntimeError("step-up needs ServerSessionMiddleware (or SessionMiddleware)")
    return session
