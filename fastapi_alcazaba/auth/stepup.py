"""Step-up: the routes that need a recent authentication, by the user who makes the request, and
of a kind (a passkey, not a code). `FastAPITOTP` and `FastAPIWebAuthn` mark the session when they
accept one; `require_recent` checks it, or the token's `auth_time`, `amr` and `acr` claims.

    totp.mark(request, user.id)  # after verify() accepted the user's code

    @app.delete(
        "/account",
        dependencies=[Depends(require_recent(current_user_id, max_age=300, methods={"passkey"}))],
    )
    async def delete_account() -> None: ...

A session that falls short gets a 403 problem saying what's needed; a token, a 401 with RFC 9470's
`insufficient_user_authentication` challenge, for the client to send the user back to the
provider with `max_age` and `acr_values`.
"""

import time
import uuid
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from fastapi_alcazaba.observability.problems import Problem

if TYPE_CHECKING:
    from fastapi_alcazaba.audit import FastAPIAudit

# Where in the session the last second factor is kept: whose, when and which.
_SESSION_KEY = "fastapi_alcazaba_mfa"
#: The problem type of a step-up refused: what the front end looks for.
PROBLEM_TYPE = "urn:fastapi-alcazaba:problem:step-up"

#: A user's ID.
UserId = str | int | uuid.UUID


def mark(request: HTTPConnection, user_id: UserId, method: str) -> None:
    """Note in the session that `user_id` just authenticated with `method` ("totp", "passkey",
    "password"...), and give the session a new id (with ServerSessionMiddleware): it holds more
    privileges now."""
    from fastapi_alcazaba.security.sessions import ServerSession, rotate_session

    session = _session(request)
    session[_SESSION_KEY] = {"user": str(user_id), "at": time.time(), "method": method}
    if isinstance(session, ServerSession):
        rotate_session(request)


def verified_at(request: HTTPConnection, user_id: UserId) -> float | None:
    """When `user_id` last gave a second factor in this session (Unix time); None when they
    didn't, or someone else did."""
    found = _marked(request, user_id)
    return found[0] if found is not None else None


def verified_with(request: HTTPConnection, user_id: UserId) -> str | None:
    """The method `user_id` last authenticated with in this session; None when they didn't, or
    someone else did."""
    found = _marked(request, user_id)
    return found[1] if found is not None else None


def require_recent(
    user_id: Callable[..., Any],
    max_age: int = 300,
    *,
    methods: Collection[str] | None = None,
    claims: Callable[..., Any] | None = None,
    acr_values: Collection[str] | None = None,
    subject: Callable[[dict[str, Any]], Any] | None = None,
    audit: "FastAPIAudit | None" = None,
) -> Callable[..., Any]:
    """A dependency that lets through the users who authenticated in the last `max_age` seconds,
    with one of `methods` when given, and returns their ID; requests without a user get a 401.

    - **Sessions:** the session's mark (`mark`) must be this user's, recent, and of one of
      `methods` ("passkey", "totp"...). Otherwise a 403 problem (`PROBLEM_TYPE`, "Second factor
      required") with `max_age` and `methods`, for the front end to ask for one and `mark` it.
    - **Tokens:** when `claims` (a dependency) gives the request's token claims (a dict), they
      decide instead: the token must be the user's (its `sub` is `user_id`'s), with `auth_time`
      within `max_age`, an `amr` among `methods` (RFC 8176's names: "hwk", "otp", "mfa"...), an
      `acr` among `acr_values`. Otherwise a 401 with
      `WWW-Authenticate: Bearer error="insufficient_user_authentication"` and the `max_age` and
      `acr_values` to ask the provider for (RFC 9470), as a problem too.
    - **Audited:** with `audit`, each check is an `auth.stepup` event with the method and the
      age (and the reason when it's refused).

    :param user_id: a dependency that returns the request's user ID, or None.
    :param claims: a dependency that returns the token's claims when the request came with one
        (`auth.authenticate("jwt", optional=True)`), None for a session.
    :param subject: the user ID a token's claims belong to, compared with `user_id`'s; the `sub`
        claim by default. A token of someone else (a fresh one of the attacker's own, sent with a
        stolen session) never steps anybody up.
    """
    if max_age < 1:
        raise ValueError("max_age is 1 second or more")
    wanted = frozenset(methods) if methods is not None else None
    acr = frozenset(acr_values) if acr_values is not None else None
    if (wanted is not None and not wanted) or (acr is not None and not acr):
        raise ValueError("methods and acr_values list at least one, or are None")
    owner = subject if subject is not None else _sub

    async def no_claims() -> None:
        return None

    async def recent(
        request: HTTPConnection,
        current: Annotated[Any, Depends(user_id)],
        token: Annotated[Any, Depends(claims if claims is not None else no_claims)],
    ) -> Any:
        if current is None:
            raise HTTPException(HTTP_401_UNAUTHORIZED, "Not authenticated")
        if isinstance(token, dict):
            method, age, reason = _token_check(token, max_age, wanted, acr)
            if str(owner(token)) != str(current):
                reason = "other_subject"
        else:
            method, age, reason = _session_check(request, current, max_age, wanted)
        if audit is not None:
            await audit.log(
                "auth.stepup",
                outcome="success" if reason is None else "failure",
                actor=current,
                reason=reason,
                details={"method": method, "age": age, "token": isinstance(token, dict)},
                request=request,
            )
        if reason is None:
            return current
        if isinstance(token, dict):
            raise _challenge(max_age, wanted, acr)
        raise Problem(
            HTTP_403_FORBIDDEN,
            "Second factor required",
            type=PROBLEM_TYPE,
            title="Step-up required",
            extensions={"max_age": max_age, "methods": sorted(wanted) if wanted else None},
        )

    return recent


def _session_check(
    request: HTTPConnection,
    user: Any,
    max_age: int,
    methods: frozenset[str] | None,
) -> tuple[str | None, int | None, str | None]:
    """(method, age in seconds, why it's refused or None)."""
    found = _marked(request, user)
    if found is None:
        return None, None, "none"
    at, method = found
    age = int(time.time() - at)
    if not 0 <= age <= max_age:
        return method, age, "too_old"
    if methods is not None and method not in methods:
        return method, age, "wrong_method"
    return method, age, None


def _sub(claims: dict[str, Any]) -> Any:
    return claims.get("sub")


def _token_check(
    claims: dict[str, Any],
    max_age: int,
    methods: frozenset[str] | None,
    acr_values: frozenset[str] | None,
) -> tuple[str | None, int | None, str | None]:
    amr = claims.get("amr")
    used = [value for value in amr if isinstance(value, str)] if isinstance(amr, list) else []
    method = ",".join(used) or None
    auth_time = claims.get("auth_time")
    if not isinstance(auth_time, int | float) or isinstance(auth_time, bool):
        return method, None, "no_auth_time"
    age = int(time.time() - auth_time)
    if not 0 <= age <= max_age:
        return method, age, "too_old"
    if methods is not None and not methods.intersection(used):
        return method, age, "wrong_method"
    if acr_values is not None and claims.get("acr") not in acr_values:
        return method, age, "wrong_acr"
    return method, age, None


def _challenge(
    max_age: int,
    methods: frozenset[str] | None,
    acr_values: frozenset[str] | None,
) -> Problem:
    """RFC 9470's challenge: what the client asks the provider for when it sends the user back."""
    parameters = [
        'error="insufficient_user_authentication"',
        'error_description="A more recent or stronger authentication is required"',
        f'max_age="{max_age}"',
    ]
    if acr_values:
        parameters.append(f'acr_values="{" ".join(sorted(acr_values))}"')
    return Problem(
        HTTP_401_UNAUTHORIZED,
        "A more recent or stronger authentication is required",
        type=PROBLEM_TYPE,
        title="Step-up required",
        headers={"WWW-Authenticate": "Bearer " + ", ".join(parameters)},
        extensions={
            "max_age": max_age,
            "methods": sorted(methods) if methods else None,
            "acr_values": sorted(acr_values) if acr_values else None,
        },
    )


def _marked(request: HTTPConnection, user_id: UserId) -> tuple[float, str | None] | None:
    found = _session(request).get(_SESSION_KEY)
    if not isinstance(found, dict) or found.get("user") != str(user_id):
        return None
    at, method = found.get("at"), found.get("method")
    if not isinstance(at, int | float) or isinstance(at, bool):
        return None
    return float(at), method if isinstance(method, str) else None


def _session(request: HTTPConnection) -> dict[str, Any]:
    session: dict[str, Any] | None = request.scope.get("session")
    if session is None:
        raise RuntimeError("step-up needs ServerSessionMiddleware (or SessionMiddleware)")
    return session
