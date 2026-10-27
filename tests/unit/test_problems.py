"""FastAPIProblemDetails and Problem: RFC 9457 answers that don't leak."""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.testclient import WebSocketDenialResponse
from starlette.types import Message

from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.observability import FastAPIProblemDetails, Problem, RequestIDMiddleware
from fastapi_alcazaba.observability.problems import MEDIA_TYPE
from fastapi_alcazaba.operations import BreakerOpen, FastAPIMaintenance, MaintenanceMiddleware
from fastapi_alcazaba.security import BodySizeLimitMiddleware


class Login(BaseModel):
    user: str
    password: str = Field(min_length=12)


class OutOfStock(Exception):
    pass


def make_app(
    problems: FastAPIProblemDetails | None = None,
    *,
    request_ids: bool = False,
) -> FastAPI:
    app = FastAPI()
    problems = problems or FastAPIProblemDetails()

    @app.get("/raise/{status}")
    async def raise_status(status: int, detail: str | None = None) -> None:
        raise HTTPException(status, detail, headers={"X-Extra": "kept"})

    @app.get("/structured")
    async def structured() -> None:
        raise HTTPException(400, detail={"code": "E42", "fields": ["name"]})

    @app.get("/breaker")
    async def breaker() -> None:
        raise BreakerOpen("payments", 2.5)

    @app.get("/redirect")
    async def redirect() -> None:
        raise HTTPException(307, headers={"Location": "/elsewhere"})

    @app.get("/not-modified")
    async def not_modified() -> None:
        raise HTTPException(304, headers={"ETag": '"v1"'})

    @app.get("/problem")
    async def problem() -> None:
        raise Problem(
            409,
            "Only 3 left",
            type="https://example.com/problems/out-of-stock",
            title="Out of stock",
            headers={"Retry-After": "60"},
            extensions={"available": 3, "restock": datetime(2026, 1, 2, tzinfo=UTC)},
        )

    @app.get("/maintenance")
    async def maintenance() -> None:
        raise Problem(503, "Back at 14:00", headers={"Retry-After": "600"})

    @app.post("/login")
    # pylint: disable-next=unused-argument  # the body FastAPI validates
    async def login(body: Login) -> None:
        pass  # pragma: no cover

    @app.post("/numbers")
    async def numbers(body: list[int]) -> int:
        return len(body)  # pragma: no cover

    @app.get("/own-validation")
    async def own_validation(kind: str) -> None:
        if kind == "partial":
            raise RequestValidationError([{"msg": "Not today", "input": "secret"}])
        raise RequestValidationError(["plain string"])

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("postgresql://admin:hunter2@db.internal/app")

    @app.get("/out-of-stock")
    async def out_of_stock() -> None:
        raise OutOfStock

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> None:
        raise HTTPException(404, "No such item")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        raise HTTPException(403, "Not for you")

    @app.exception_handler(OutOfStock)
    async def handle_out_of_stock(request: Request, exc: Exception) -> Response:
        return problems.response(request, Problem(409, title="Out of stock"))

    problems.install(app)
    if request_ids:
        app.add_middleware(RequestIDMiddleware)
    return app


@pytest.fixture(name="client")
def fixture_client() -> TestClient:
    return TestClient(make_app(), raise_server_exceptions=False)


class TestHTTPExceptions:
    def test_a_4xx_with_its_detail(self, client: TestClient) -> None:
        response = client.get("/raise/404", params={"detail": "No such order"})
        assert response.status_code == 404
        assert response.headers["content-type"] == MEDIA_TYPE
        assert response.headers["x-extra"] == "kept"
        assert response.json() == {
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": "No such order",
            "instance": "/raise/404",
        }
        assert "x-request-id" not in response.headers  # no RequestIDMiddleware

    def test_a_detail_that_only_repeats_the_status(self, client: TestClient) -> None:
        assert "detail" not in client.get("/raise/404").json()  # Starlette's default: the phrase
        assert "detail" not in client.get("/raise/400", params={"detail": ""}).json()

    def test_hidden_4xx_details(self) -> None:
        client = TestClient(make_app(FastAPIProblemDetails(expose_detail=False)))
        body = client.get("/raise/403", params={"detail": "user 42 isn't an admin"}).json()
        assert body == {
            "type": "about:blank",
            "title": "Forbidden",
            "status": 403,
            "instance": "/raise/403",
        }
        assert "errors" not in client.get("/structured").json()

    def test_a_5xx_never_shows_its_detail(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.observability.problems"):
            response = client.get("/raise/500", params={"detail": "db down at 10.0.0.7"})
        assert response.json() == {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "instance": "/raise/500",
        }
        assert response.headers["x-extra"] == "kept"
        # Logged instead, for whoever raised it.
        assert "GET /raise/{status} answered 500" in caplog.text
        assert "db down at 10.0.0.7" in caplog.text

    def test_breaker_open(self, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.observability.problems"):
            response = client.get("/breaker")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "3"
        assert response.json() == {
            "type": "about:blank",
            "title": "Service Unavailable",
            "status": 503,
            "instance": "/breaker",
        }
        assert caplog.text == ""  # its detail is the phrase: nothing hidden

    def test_a_structured_detail_goes_in_errors(self, client: TestClient) -> None:
        body = client.get("/structured").json()
        assert "detail" not in body
        assert body["errors"] == {"code": "E42", "fields": ["name"]}

    def test_codes_under_400_keep_fastapis_answer(self, client: TestClient) -> None:
        response = client.get("/redirect", follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (307, "/elsewhere")
        assert response.json() == {"detail": "Temporary Redirect"}
        response = client.get("/not-modified")
        assert (response.status_code, response.content) == (304, b"")
        assert response.headers["etag"] == '"v1"'

    def test_head_requests(self, client: TestClient) -> None:
        response = client.head("/items/a")  # FastAPI routes don't take HEAD: a 405
        assert response.status_code == 405
        assert response.headers["allow"] == "GET"
        assert response.headers["content-type"] == MEDIA_TYPE
        assert response.content == b""

    def test_unknown_routes(self, client: TestClient) -> None:
        response = client.get("/nowhere")
        assert (response.status_code, response.json()["title"]) == (404, "Not Found")

    @pytest.mark.parametrize(
        ("path", "instance"),
        [
            ("/items/a%20b", "/items/a%20b"),
            ("/items/%0Aforged", "/items/%0Aforged"),
            ("/items/caf%C3%A9", "/items/caf%C3%A9"),
            ("/items/100%25", "/items/100%25"),
            ("/items/a:b@c;d=e", "/items/a:b@c;d=e"),
        ],
    )
    def test_the_instance_is_a_uri_reference(
        self,
        client: TestClient,
        path: str,
        instance: str,
    ) -> None:
        assert client.get(path).json()["instance"] == instance

    def test_a_status_handler_of_the_app_still_wins(self) -> None:
        app = make_app()

        @app.exception_handler(404)
        async def not_found(request: Request, exc: Exception) -> Response:
            return Response("custom", 404)

        assert TestClient(app).get("/raise/404").text == "custom"

    def test_websockets_are_denied_with_a_problem(self, client: TestClient) -> None:
        with pytest.raises(WebSocketDenialResponse) as exc_info:
            with client.websocket_connect("/ws"):
                pass  # pragma: no cover
        denial = exc_info.value
        assert denial.status_code == 403
        assert denial.json() == {
            "type": "about:blank",
            "title": "Forbidden",
            "status": 403,
            "detail": "Not for you",
            "instance": "/ws",
        }

    @pytest.mark.parametrize(
        ("status", "title"),
        [
            (413, "Content Too Large"),  # the RFC 9110 phrases, whatever the Python version
            (414, "URI Too Long"),
            (416, "Range Not Satisfiable"),
            (418, "I'm a Teapot"),
            (499, "Client Error"),  # no phrase
            (599, "Server Error"),
        ],
    )
    def test_titles(self, client: TestClient, status: int, title: str) -> None:
        assert client.get(f"/raise/{status}", params={"detail": "x"}).json()["title"] == title


class TestProblem:
    def test_type_title_extensions_and_headers(self, client: TestClient) -> None:
        response = client.get("/problem")
        assert response.status_code == 409
        assert response.headers["retry-after"] == "60"
        assert response.json() == {
            "type": "https://example.com/problems/out-of-stock",
            "title": "Out of stock",
            "status": 409,
            "detail": "Only 3 left",
            "instance": "/problem",
            "available": 3,
            "restock": "2026-01-02T00:00:00+00:00",
        }

    def test_its_detail_is_sent_even_with_a_5xx(self, client: TestClient) -> None:
        assert client.get("/maintenance").json()["detail"] == "Back at 14:00"
        hiding = TestClient(make_app(FastAPIProblemDetails(expose_detail=False)))
        assert hiding.get("/problem").json()["detail"] == "Only 3 left"

    def test_defaults(self) -> None:
        problem = Problem(422)
        assert (problem.type, problem.title, problem.extensions) == (
            "about:blank",
            "Unprocessable Content",
            {},
        )
        assert (problem.status_code, problem.headers) == (422, None)
        assert Problem(499).title == "Client Error"

    def test_in_your_own_handlers(self, client: TestClient) -> None:
        assert client.get("/out-of-stock").json() == {
            "type": "about:blank",
            "title": "Out of stock",
            "status": 409,
            "instance": "/out-of-stock",
        }

    @pytest.mark.parametrize("status", [200, 302, 399, 600, 99])
    def test_only_error_codes(self, status: int) -> None:
        with pytest.raises(ValueError, match="4xx or 5xx"):
            _ = Problem(status)

    @pytest.mark.parametrize("type_", ["", "has space", "https://x/\nInjected", "a\tb"])
    def test_the_type_is_a_uri(self, type_: str) -> None:
        with pytest.raises(ValueError, match="URI"):
            _ = Problem(400, type=type_)

    @pytest.mark.parametrize("name", ["ab", "1abc", "a-b", "with space", "_abc", "ñandú"])
    def test_extension_names(self, name: str) -> None:
        with pytest.raises(ValueError, match="invalid extension member name"):
            _ = Problem(400, extensions={name: 1})

    @pytest.mark.parametrize(
        "name", ["type", "title", "status", "detail", "instance", "request_id"]
    )
    def test_extensions_cant_replace_standard_members(self, name: str) -> None:
        with pytest.raises(ValueError, match="standard member"):
            _ = Problem(400, extensions={name: 1})

    def test_extension_values_are_checked_when_raised(self) -> None:
        with pytest.raises(ValueError):  # noqa: PT011 - FastAPI's message, not ours
            _ = Problem(400, extensions={"what": object()})


class TestValidationErrors:
    def test_the_values_sent_are_never_echoed(self, client: TestClient) -> None:
        response = client.post("/login", json={"password": "hunter2"})
        assert response.status_code == 422
        assert response.headers["content-type"] == MEDIA_TYPE
        assert "hunter2" not in response.text
        body = response.json()
        assert body == {
            "type": "about:blank",
            "title": "Unprocessable Content",
            "status": 422,
            "instance": "/login",
            "errors": [
                {"loc": ["body", "user"], "msg": "Field required", "type": "missing"},
                {
                    "loc": ["body", "password"],
                    "msg": "String should have at least 12 characters",
                    "type": "string_too_short",
                },
            ],
        }

    def test_huge_bodies_get_a_bounded_answer(self) -> None:
        client = TestClient(make_app(FastAPIProblemDetails(max_errors=3)))
        body = client.post("/numbers", json=["x"] * 50).json()
        assert [error["loc"] for error in body["errors"]] == [["body", i] for i in range(3)]
        assert body["detail"] == "50 validation errors; the first 3 are listed"

    def test_errors_raised_by_the_app(self, client: TestClient) -> None:
        partial = client.get("/own-validation", params={"kind": "partial"}).json()
        assert partial["errors"] == [{"loc": [], "msg": "Not today", "type": ""}]
        plain = client.get("/own-validation", params={"kind": "plain"}).json()
        assert plain["errors"] == [{"loc": [], "msg": "plain string", "type": "value_error"}]

    def test_locations_that_arent_strings_or_numbers(self) -> None:
        app = FastAPI()

        @app.get("/")
        async def endpoint() -> None:
            raise RequestValidationError([{"loc": ("query", 1, ("x",)), "msg": "m", "type": "t"}])

        FastAPIProblemDetails().install(app)
        assert TestClient(app).get("/").json()["errors"][0]["loc"] == ["query", 1, "('x',)"]

    def test_max_errors_is_positive(self) -> None:
        with pytest.raises(ValueError, match="max_errors"):
            FastAPIProblemDetails(max_errors=0)


class TestUnhandledExceptions:
    def test_a_500_that_says_nothing(self, client: TestClient) -> None:
        response = client.get("/boom")
        assert response.status_code == 500
        assert response.headers["content-type"] == MEDIA_TYPE
        assert response.json() == {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "instance": "/boom",
        }
        assert "hunter2" not in response.text

    def test_raised_again_for_the_server_and_the_tests(self) -> None:
        """ServerErrorMiddleware raises it after the response: servers log it, tests see it."""
        with pytest.raises(RuntimeError, match="hunter2"):
            TestClient(make_app()).get("/boom")

    async def test_the_server_gets_the_response_and_then_the_exception(self) -> None:
        messages: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b""}  # pragma: no cover

        async def send(message: Message) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/boom",
            "raw_path": b"/boom",
            "query_string": b"",
            "headers": [],
        }
        with pytest.raises(RuntimeError):
            await make_app()(scope, receive, send)
        assert messages[0]["status"] == 500
        assert b"hunter2" not in messages[1]["body"]


class TestRequestIDs:
    def test_in_every_problem(self) -> None:
        client = TestClient(make_app(request_ids=True), raise_server_exceptions=False)
        for path in ("/raise/404", "/boom", "/problem"):
            response = client.get(path, headers={"X-Request-ID": "req-1"})
            assert response.json()["request_id"] == "req-1"
            assert response.headers.get_list("x-request-id") == ["req-1"]

    def test_validation_errors(self) -> None:
        client = TestClient(make_app(request_ids=True))
        response = client.post("/login", json={}, headers={"X-Request-ID": "req-2"})
        assert response.json()["request_id"] == "req-2"
        assert response.headers.get_list("x-request-id") == ["req-2"]

    def test_a_500_generated_id(self) -> None:
        """The 500 is answered outside RequestIDMiddleware: the handler adds the header."""
        client = TestClient(make_app(request_ids=True), raise_server_exceptions=False)
        response = client.get("/boom")
        assert response.json()["request_id"] == response.headers["x-request-id"]

    def test_the_header_of_an_exception_is_kept(self) -> None:
        app = FastAPI()

        @app.get("/")
        async def endpoint() -> None:
            raise HTTPException(400, headers={"x-request-id": "from-the-exception"})

        FastAPIProblemDetails().install(app)
        app.add_middleware(RequestIDMiddleware)
        response = TestClient(app).get("/", headers={"X-Request-ID": "req-3"})
        assert response.headers.get_list("x-request-id") == ["from-the-exception"]
        assert response.json()["request_id"] == "req-3"


def test_install_keeps_handlers_for_subclasses() -> None:
    app = FastAPI()

    class Teapot(HTTPException):
        pass

    @app.get("/")
    async def endpoint() -> None:
        raise Teapot(418)

    @app.exception_handler(Teapot)
    async def teapot(request: Request, exc: Exception) -> Response:
        return Response("short and stout", 418)

    FastAPIProblemDetails().install(app)
    assert TestClient(app).get("/").text == "short and stout"


def test_problems_on_starlette_apps() -> None:
    from starlette.applications import Starlette
    from starlette.routing import Route

    async def endpoint(request: Request) -> Response:
        raise HTTPException(401, "Log in first", headers={"WWW-Authenticate": "Bearer"})

    app = Starlette(routes=[Route("/", endpoint)])
    FastAPIProblemDetails().install(app)
    response = TestClient(app).get("/")
    assert response.headers["www-authenticate"] == "Bearer"
    body: dict[str, Any] = response.json()
    assert (body["status"], body["detail"]) == (401, "Log in first")


class TestMiddlewareAnswers:
    """Alcazaba's middlewares answer with problems too when the app has them installed."""

    @staticmethod
    def app(maintenance: FastAPIMaintenance, *, problems: bool) -> FastAPI:
        app = FastAPI()

        @app.post("/echo")
        async def echo(request: Request) -> int:
            return len(await request.body())

        if problems:
            FastAPIProblemDetails().install(app)
        app.add_middleware(BodySizeLimitMiddleware, max_size=4)
        app.add_middleware(MaintenanceMiddleware, maintenance=maintenance)
        app.add_middleware(RequestIDMiddleware)
        return app

    def test_a_body_too_large(self) -> None:
        app = self.app(FastAPIMaintenance(Cache(InMemoryBackend())), problems=True)
        response = TestClient(app).post("/echo", content=b"12345")
        assert response.headers["content-type"] == MEDIA_TYPE
        assert response.json() == {
            "type": "about:blank",
            "title": "Content Too Large",
            "status": 413,
            "detail": "Request body too large",
            "instance": "/echo",
            "request_id": response.headers["x-request-id"],
        }

    async def test_maintenance_keeps_its_headers(self) -> None:
        maintenance = FastAPIMaintenance(Cache(InMemoryBackend()))
        await maintenance.enable("Back at 10:30", retry_after=900)
        response = TestClient(self.app(maintenance, problems=True)).post("/echo", content=b"1")
        assert response.status_code == 503
        assert (response.headers["retry-after"], response.headers["cache-control"]) == (
            "900",
            "no-store",
        )
        assert response.json()["detail"] == "Back at 10:30"
        assert response.headers["content-type"] == MEDIA_TYPE

    async def test_without_problems_fastapis_answer(self) -> None:
        maintenance = FastAPIMaintenance(Cache(InMemoryBackend()))
        client = TestClient(self.app(maintenance, problems=False))
        response = client.post("/echo", content=b"12345")
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"detail": "Request body too large"}
        await maintenance.enable(retry_after=60)
        response = client.post("/echo", content=b"1")
        assert (response.status_code, response.json()) == (503, {"detail": "Service Unavailable"})
        assert response.headers["retry-after"] == "60"
