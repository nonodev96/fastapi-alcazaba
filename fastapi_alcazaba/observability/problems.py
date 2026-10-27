"""Errors as RFC 9457 problem details (`application/problem+json`) instead of FastAPI's
`{"detail": ...}`, without telling clients what they shouldn't know.

    problems = FastAPIProblemDetails()
    problems.install(app)

    raise Problem(
        409,
        "Only 3 left in stock",
        type="https://example.com/problems/out-of-stock",
        title="Out of stock",
        extensions={"available": 3},
    )

A response has `type` ("about:blank" unless a Problem says otherwise), `title` (the status
phrase), `status`, `detail` when there's one to tell, `instance` (the request's path) and
`request_id` when RequestIDMiddleware gave the request one. An unhandled exception is a 500 that
says nothing about it (the server logs it with its traceback); validation errors list where and
what, never the values that were sent (FastAPI echoes them, passwords included).
"""

import http.client
import logging
import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

from fastapi_alcazaba.core.asgi import RATE_LIMIT_HEADERS
from fastapi_alcazaba.observability.request_id import _current_header
from fastapi_alcazaba.response_cache.caches import metrics_of

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

MEDIA_TYPE = "application/problem+json"

_STANDARD_MEMBERS = frozenset({"type", "title", "status", "detail", "instance", "request_id"})
# RFC 9457 §3.2: extension names start with a letter, have 3 or more letters, digits or "_".
_EXTENSION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_URI_REFERENCE = re.compile(r"[^\s\x00-\x1f\x7f]+")
# The RFC 9110 phrases; before Python 3.13, http.HTTPStatus has the RFC 7231 ones.
_RFC_9110_TITLES = {
    413: "Content Too Large",
    414: "URI Too Long",
    416: "Range Not Satisfiable",
    422: "Unprocessable Content",
}
# Characters of a URI path that stay as they are (RFC 3986 pchar and "/").
_PATH_SAFE = "/:@!$&'()*+,;="


def _title(status: int) -> str:
    """The status phrase, which RFC 9457 recommends as the title of "about:blank" problems."""
    if status in _RFC_9110_TITLES:
        return _RFC_9110_TITLES[status]
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Client Error" if status < 500 else "Server Error"


def _says_nothing(detail: str, status: int) -> bool:
    """Whether a detail only repeats the status phrase (Starlette's default detail)."""
    return not detail or detail in (_title(status), http.client.responses.get(status))


def _where(scope: Scope) -> str:
    """The method and route template, for logs; when no route matched, the path quoted with
    repr(): it comes from the client and may hold line breaks."""
    template = getattr(scope.get("route"), "path_format", None)
    path = template if isinstance(template, str) else repr(scope.get("path", ""))
    return f"{scope.get('method', 'WEBSOCKET')} {path}"


def _listed(error: Any) -> dict[str, Any]:
    """A validation error without its `input` and `ctx`, which carry the values sent."""
    if not isinstance(error, Mapping):  # a RequestValidationError raised by the app itself
        return {"loc": [], "msg": str(error), "type": "value_error"}
    loc = [part if isinstance(part, int | str) else str(part) for part in error.get("loc", ())]
    return {
        "loc": loc,
        "msg": str(error.get("msg", "")),
        "type": str(error.get("type", "")),
    }


class Problem(HTTPException):
    """An HTTPException with the members of an RFC 9457 problem: its `type` (a URI that
    identifies the kind of problem, ideally to its documentation), `title`, `detail` and
    extension members. Its detail and extensions always reach the client, also with a 5xx: you
    write them for it.

        raise Problem(403, "Your balance is 30, but that costs 50",
                      type="https://example.com/problems/out-of-credit", title="Out of credit",
                      extensions={"balance": 30, "cost": 50})

    :param status: a 4xx or 5xx code.
    :param detail: what happened this time, for the client to read.
    :param type: the problem type's URI; "about:blank" means the status says it all.
    :param title: a short summary of the problem type; the status phrase by default.
    :param headers: response headers, such as `Retry-After`.
    :param extensions: more members, with names of three or more letters, digits or "_"
        (starting with a letter) and values that FastAPI can encode as JSON; they can't
        replace the standard members.
    """

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        type: str = "about:blank",  # pylint: disable=redefined-builtin  # RFC 9457's member name
        title: str | None = None,
        headers: Mapping[str, str] | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> None:
        if not 400 <= status <= 599:
            raise ValueError(f"a problem's status is a 4xx or 5xx code, not {status}")
        if not _URI_REFERENCE.fullmatch(type):
            raise ValueError(f"the type must be a URI: {type!r}")
        members: dict[str, Any] = {}
        for name, value in (extensions or {}).items():
            if not _EXTENSION_NAME.fullmatch(name):
                raise ValueError(f"invalid extension member name: {name!r}")
            if name in _STANDARD_MEMBERS:
                raise ValueError(f"{name!r} is a standard member, not an extension")
            members[name] = jsonable_encoder(value)  # fails here, not in the error handler
        self.type = type
        self.title = title if title is not None else _title(status)
        self.extensions = members
        # Never None: before Starlette 0.47, None looked the code up in HTTPStatus and failed
        # for codes it doesn't know.
        super().__init__(status, detail if detail is not None else self.title, headers)


class FastAPIProblemDetails:
    """Answers errors with RFC 9457 problem details. `install(app)` handles:

    - `HTTPException` (FastAPI's and Starlette's, and subclasses such as `BreakerOpen` and
      `Problem`), keeping its headers (`Retry-After`, `WWW-Authenticate`...). A 4xx's detail is
      sent, since it's written for clients (`expose_detail=False` keeps it); a 5xx's never is,
      except a Problem's. Codes under 400 (redirects raised as exceptions) keep FastAPI's answer.
    - `RequestValidationError`: a 422 whose `errors` list each error's `loc`, `msg` and `type`,
      never its `input` (which echoes what was sent, passwords included) nor its `ctx`.
    - any other exception: a 500 that says nothing about it. Starlette raises the exception
      again after the response, so that the server logs it with its traceback (uvicorn's
      "Exception in ASGI application") and TestClient raises it in tests
      (`raise_server_exceptions=False` to see the response). With `debug=True`, Starlette
      answers with its traceback page instead.

        FastAPIProblemDetails().install(app)

    :param expose_detail: send the detail of 4xx HTTPExceptions.
    :param max_errors: validation errors listed at most, so that a huge invalid body doesn't
        get a huge answer.
    """

    def __init__(
        self,
        *,
        expose_detail: bool = True,
        max_errors: int = 100,
    ) -> None:
        if max_errors < 1:
            raise ValueError("max_errors must be at least 1")
        self.expose_detail = expose_detail
        self.max_errors = max_errors

    def install(self, app: Starlette) -> None:
        """Register the handlers in `app`, before it starts. A handler you register for a
        status code or an exception subclass still wins for those. Alcazaba's middlewares answer
        with problems too (the 504 of a timeout, the 413 of a body too large...)."""
        app.state.fastapi_alcazaba_problems = self  # what the middlewares look for
        app.add_exception_handler(HTTPException, self._http_exception)
        app.add_exception_handler(RequestValidationError, self._validation_error)
        # Starlette gives the handlers of Exception (or 500) to ServerErrorMiddleware, around
        # every middleware: it answers with this one and then raises the exception again.
        app.add_exception_handler(Exception, self._unhandled)

    def response(self, request: Request, exc: HTTPException) -> Response:
        """The answer to `exc`, for your own exception handlers:

            @app.exception_handler(OutOfStock)
            async def out_of_stock(request: Request, exc: OutOfStock) -> Response:
                return problems.response(request, Problem(409, title="Out of stock"))

        The same rules apply as for a raised exception.
        """
        status = exc.status_code
        kind = "problem" if isinstance(exc, Problem) else "http_exception"
        metrics_of(request).count("problems", status=str(status), kind=kind)
        if isinstance(exc, Problem):
            return self._problem(
                request,
                status,
                problem_type=exc.type,
                title=exc.title,
                detail=exc.detail,
                extensions=exc.extensions,
                headers=exc.headers,
            )
        detail: Any = exc.detail
        extensions: dict[str, Any] = {}
        if status >= 500:
            if not (isinstance(detail, str) and _says_nothing(detail, status)):
                # Hidden from the client, but whoever raised it wanted it read somewhere.
                logger.warning(
                    "%s answered %d; its detail isn't sent to the client: %r",
                    _where(request.scope),
                    status,
                    detail,
                )
            detail = None
        elif not self.expose_detail:
            detail = None
        elif not isinstance(detail, str):
            # RFC 9457's detail is a string: a dict or a list goes in "errors".
            extensions["errors"] = jsonable_encoder(detail)
            detail = None
        return self._problem(
            request, status, detail=detail, extensions=extensions, headers=exc.headers
        )

    def _problem(
        self,
        request: Request,
        status: int,
        *,
        problem_type: str = "about:blank",
        title: str | None = None,
        detail: str | None = None,
        extensions: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        body: dict[str, Any] = {
            "type": problem_type,
            "title": title or _title(status),
            "status": status,
        }
        if detail is not None and not _says_nothing(detail, status) and detail != body["title"]:
            body["detail"] = detail
        # Percent-encoded: the path in the scope is decoded, and `instance` is a URI reference.
        body["instance"] = quote(request.scope.get("path", ""), safe=_PATH_SAFE)
        body.update(extensions or {})
        response_headers = dict(headers or {})
        if (current := _current_header()) is not None:
            name, value = current
            body["request_id"] = value
            # The 500 doesn't go through RequestIDMiddleware: it's answered outside of it.
            if all(key.lower() != name.lower() for key in response_headers):
                response_headers[name] = value
        response = JSONResponse(body, status, headers=response_headers, media_type=MEDIA_TYPE)
        # The rate limiters' headers, which a response built from an exception doesn't get.
        for name, value in request.scope.get("state", {}).get(RATE_LIMIT_HEADERS, ()):
            response.headers.append(name, value)
        return response

    async def _http_exception(self, request: Request, exc: Exception) -> Response:
        http_exc = cast(HTTPException, exc)
        if http_exc.status_code < 400:
            return await http_exception_handler(request, http_exc)
        return self.response(request, http_exc)

    async def _validation_error(self, request: Request, exc: Exception) -> Response:
        errors = cast(RequestValidationError, exc).errors()
        metrics_of(request).count("problems", status="422", kind="validation")
        listed = [_listed(error) for error in errors[: self.max_errors]]
        detail = None
        if len(errors) > self.max_errors:
            detail = f"{len(errors)} validation errors; the first {self.max_errors} are listed"
        return self._problem(request, 422, detail=detail, extensions={"errors": listed})

    async def _unhandled(self, request: Request, exc: Exception) -> Response:
        # Not logged here: ServerErrorMiddleware raises it again after this response, and the
        # server logs it with its traceback (and its request ID: RequestIDMiddleware leaves it
        # set). Logging it here too would double every traceback, and every error report.
        metrics_of(request).count("problems", status="500", kind="unhandled")
        return self._problem(request, 500)
