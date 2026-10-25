"""ResilientTransport: a deadline for the whole call and a circuit breaker per origin."""

import asyncio
import gzip
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx2 as httpx
import pytest

from fastapi_alcazaba.outbound import CircuitOpenError, ResilientTransport, ResponseTooLargeError

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class Clock:
    """Stands in for the `time` module of fastapi_alcazaba.resilience, and only there: the event
    loop keeps the real time.monotonic."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("fastapi_alcazaba.resilience.time", clock)
    return clock


class Server:
    """A MockTransport handler that counts the requests it gets."""

    def __init__(self, status: int = 200, *, error: bool = False) -> None:
        self.status = status
        self.error = error
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.error:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(self.status, text="ok")


class Trickle(httpx.AsyncByteStream):
    """A body that arrives a chunk at a time."""

    def __init__(self, chunks: list[bytes], delay: float) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def client(handler: Handler, **options: object) -> httpx.AsyncClient:
    transport = ResilientTransport(httpx.MockTransport(handler), **options)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="https://api.example")


class TestDeadline:
    async def test_responses_within_it(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.content == b"payload"
            return httpx.Response(
                201,
                headers={"X-Id": "7"},
                stream=Trickle([b"cre", b"ated"], 0),
                extensions={"http_version": b"HTTP/2", "network_stream": object()},
            )

        async with client(handler, timeout=1) as api:
            response = await api.post("/items", content=b"payload")
        assert (response.status_code, response.headers["x-id"], response.text) == (
            201,
            "7",
            "created",
        )
        assert response.http_version == "HTTP/2"
        assert "network_stream" not in response.extensions  # its connection is closed

    async def test_a_slow_server(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")  # pragma: no cover

        async with client(handler, timeout=0.05) as api:
            with pytest.raises(httpx.TimeoutException, match="api.example: no complete response"):
                await api.get("/")

    async def test_a_body_that_trickles(self) -> None:
        """httpx's read timeout restarts with every byte: this one never fires."""
        body = Trickle([b"x"] * 100, 0.01)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=body)

        async with client(handler, timeout=0.1) as api:
            with pytest.raises(httpx.TimeoutException):
                await api.get("/")
        assert body.closed

    async def test_compressed_bodies_are_decoded_once(self) -> None:
        compressed = gzip.compress(b"hello")

        async def handler(request: httpx.Request) -> httpx.Response:
            stream = Trickle([compressed[:5], compressed[5:]], 0)
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

        async with client(handler) as api:
            assert (await api.get("/")).text == "hello"

    async def test_responses_already_in_memory_go_through(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"hi")
            )

        async with client(handler) as api:
            assert (await api.get("/")).text == "hi"

    async def test_none(self) -> None:
        async with client(Server(), timeout=None) as api:
            assert (await api.get("/")).text == "ok"


class TestCircuitBreaker:
    async def test_failures_open_it(self, caplog: pytest.LogCaptureFixture) -> None:
        server = Server(error=True)
        async with client(server, failure_threshold=2) as api:
            for _ in range(2):
                with pytest.raises(httpx.ConnectError):
                    await api.get("/")
            caplog.clear()
            with caplog.at_level(logging.WARNING, "fastapi_alcazaba.outbound"):
                for _ in range(3):
                    with pytest.raises(CircuitOpenError, match="https://api.example: the circuit"):
                        await api.get("/")
            transport = api._transport
        assert server.calls == 2
        assert isinstance(transport, ResilientTransport)
        assert transport.state("https://api.example/other/path") == "open"
        assert caplog.messages == []  # it warns when it opens, not on every refusal

    async def test_it_warns_when_it_opens(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.outbound"):
            async with client(Server(error=True), failure_threshold=1, reset_timeout=7) as api:
                with pytest.raises(httpx.ConnectError):
                    await api.get("/")
        assert caplog.messages == ["Circuit breaker of https://api.example open: no calls for 7 s"]

    def test_it_is_a_transport_error(self) -> None:
        """Code that handles httpx errors handles it too."""
        assert issubclass(CircuitOpenError, httpx.TransportError)

    async def test_failure_statuses_count_and_are_returned(self) -> None:
        server = Server(503)
        async with client(server, failure_threshold=2) as api:
            assert [(await api.get("/")).status_code for _ in range(2)] == [503, 503]
            with pytest.raises(CircuitOpenError):
                await api.get("/")
        assert server.calls == 2

    async def test_other_statuses_are_successes(self) -> None:
        server = Server(503)
        async with client(server, failure_threshold=2) as api:
            await api.get("/")
            server.status = 404
            await api.get("/")  # resets the count
            server.status = 503
            await api.get("/")
            assert (await api.get("/")).status_code == 503
        assert server.calls == 4

    async def test_custom_failure_statuses(self) -> None:
        async with client(Server(429), failure_threshold=1, failure_statuses={429}) as api:
            assert (await api.get("/")).status_code == 429
            with pytest.raises(CircuitOpenError):
                await api.get("/")

    async def test_deadlines_count(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")  # pragma: no cover

        async with client(handler, timeout=0.01, failure_threshold=1) as api:
            with pytest.raises(httpx.TimeoutException):
                await api.get("/")
            with pytest.raises(CircuitOpenError):
                await api.get("/")

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadError("connection reset"),
            httpx.RemoteProtocolError("server disconnected"),
            httpx.ProxyError("bad gateway"),
            httpx.ReadTimeout("httpx's own timeout"),
        ],
    )
    async def test_transport_errors_count(self, error: httpx.TransportError) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise error

        async with client(handler, failure_threshold=1) as api:
            with pytest.raises(type(error)):
                await api.get("/")
            with pytest.raises(CircuitOpenError):
                await api.get("/")

    async def test_an_illegal_header_doesnt_count(self) -> None:
        """h11 refuses it before sending: it says nothing about the origin."""
        server = Server()

        async def handler(request: httpx.Request) -> httpx.Response:
            if "x-query" in request.headers:
                raise httpx.LocalProtocolError("Illegal header value b'a\\nb'", request=request)
            return await server(request)

        async with client(handler, failure_threshold=1) as api:
            for _ in range(3):
                with pytest.raises(httpx.LocalProtocolError):
                    await api.get("/", headers={"X-Query": "a b"})
            assert (await api.get("/")).status_code == 200

    async def test_an_unsupported_scheme_doesnt_count(self) -> None:
        transport = ResilientTransport(failure_threshold=1)
        async with httpx.AsyncClient(transport=transport) as api:
            for _ in range(2):
                with pytest.raises(httpx.UnsupportedProtocol):
                    await api.get("ftp://api.example/")
        assert transport.state("ftp://api.example") == "closed"

    async def test_a_request_body_that_raises_doesnt_count(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await request.aread()
            return httpx.Response(200)  # pragma: no cover

        async def body() -> AsyncIterator[bytes]:
            yield b"x"
            raise ValueError("a bug in the caller's generator")

        async with client(handler, failure_threshold=1) as api:
            with pytest.raises(ValueError, match="caller's generator"):
                await api.post("/", content=body())
            assert api._transport.state("https://api.example") == "closed"  # type: ignore[attr-defined]

    async def test_a_response_too_large_doesnt_count(self) -> None:
        """The server did answer."""
        calls: list[int] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, stream=Trickle([b"too large"], 0))

        async with client(handler, failure_threshold=1, max_response_size=1) as api:
            for _ in range(2):
                with pytest.raises(ResponseTooLargeError):
                    await api.get("/")
        assert len(calls) == 2

    async def test_errors_that_dont_count_give_the_trial_back(self, clock: Clock) -> None:
        server = Server(error=True)

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/bad":
                raise httpx.LocalProtocolError("Illegal header value", request=request)
            return await server(request)

        async with client(handler, failure_threshold=1) as api:
            with pytest.raises(httpx.ConnectError):
                await api.get("/")
            clock.now += 30
            with pytest.raises(httpx.LocalProtocolError):
                await api.get("/bad")  # the trial, with no verdict on the origin
            assert api._transport.state("https://api.example") == "half-open"  # type: ignore[attr-defined]
            server.error = False
            assert (await api.get("/")).status_code == 200  # another trial
            assert api._transport.state("https://api.example") == "closed"  # type: ignore[attr-defined]

    async def test_a_successful_trial_closes_it(self, clock: Clock) -> None:
        server = Server(error=True)
        async with client(server, failure_threshold=1, reset_timeout=30) as api:
            with pytest.raises(httpx.ConnectError):
                await api.get("/")
            clock.now += 29
            with pytest.raises(CircuitOpenError):
                await api.get("/")
            clock.now += 1
            assert api._transport.state("https://api.example") == "half-open"  # type: ignore[attr-defined]
            server.error = False
            assert [(await api.get("/")).status_code for _ in range(3)] == [200] * 3
            assert api._transport.state("https://api.example") == "closed"  # type: ignore[attr-defined]
        assert server.calls == 4

    async def test_a_failed_trial_opens_it_again(
        self, clock: Clock, caplog: pytest.LogCaptureFixture
    ) -> None:
        server = Server(error=True)
        async with client(server, failure_threshold=3, reset_timeout=30) as api:
            for _ in range(3):
                with pytest.raises(httpx.ConnectError):
                    await api.get("/")
            clock.now += 30
            caplog.clear()
            with caplog.at_level(logging.WARNING, "fastapi_alcazaba.outbound"):
                with pytest.raises(httpx.ConnectError):
                    await api.get("/")  # the trial: one failure is enough
                with pytest.raises(CircuitOpenError):
                    await api.get("/")
        assert server.calls == 4
        assert len(caplog.messages) == 1  # open again

    async def test_one_trial_at_a_time(self, clock: Clock) -> None:
        release = asyncio.Event()
        server = Server(error=True)

        async def handler(request: httpx.Request) -> httpx.Response:
            if server.error:
                return await server(request)
            await release.wait()
            return httpx.Response(200)

        async with client(handler, failure_threshold=1) as api:
            with pytest.raises(httpx.ConnectError):
                await api.get("/")
            clock.now += 30
            server.error = False
            trial = asyncio.create_task(api.get("/"))
            await asyncio.sleep(0.01)
            with pytest.raises(CircuitOpenError):
                await api.get("/")
            release.set()
            assert (await trial).status_code == 200

    async def test_a_cancelled_trial_lets_the_next_call_try(self, clock: Clock) -> None:
        server = Server(error=True)

        async def handler(request: httpx.Request) -> httpx.Response:
            if server.error:
                return await server(request)
            await asyncio.sleep(10)
            raise AssertionError("unreachable")  # pragma: no cover

        async with client(handler, failure_threshold=1, timeout=None) as api:
            with pytest.raises(httpx.ConnectError):
                await api.get("/")
            clock.now += 30
            server.error = False
            trial = asyncio.create_task(api.get("/"))
            await asyncio.sleep(0.01)
            trial.cancel()
            with pytest.raises(asyncio.CancelledError):
                await trial
            server.error = True
            with pytest.raises(httpx.ConnectError):  # sent, not refused
                await api.get("/")

    async def test_cancelling_a_call_that_isnt_the_trial_keeps_it(self, clock: Clock) -> None:
        """A call sent while it was closed and cancelled during the trial doesn't let a second
        trial through."""
        sent: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request.url.path)
            if request.url.path == "/fail":
                raise httpx.ConnectError("connection refused", request=request)
            await asyncio.sleep(10)
            raise AssertionError("unreachable")  # pragma: no cover

        async with client(handler, failure_threshold=1, timeout=None) as api:
            slow = asyncio.create_task(api.get("/slow"))
            await asyncio.sleep(0.01)
            with pytest.raises(httpx.ConnectError):
                await api.get("/fail")
            clock.now += 30
            trial = asyncio.create_task(api.get("/trial"))
            await asyncio.sleep(0.01)
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow
            with pytest.raises(CircuitOpenError):
                await api.get("/second-trial")
            trial.cancel()
            with pytest.raises(asyncio.CancelledError):
                await trial
        assert sent == ["/slow", "/fail", "/trial"]

    async def test_each_origin_has_its_own(self) -> None:
        down, up = Server(error=True), Server()

        async def handler(request: httpx.Request) -> httpx.Response:
            origin = (request.url.scheme, request.url.host, request.url.port)
            return await (down if origin == ("https", "down.example", None) else up)(request)

        transport = ResilientTransport(httpx.MockTransport(handler), failure_threshold=1)
        async with httpx.AsyncClient(transport=transport) as api:
            with pytest.raises(httpx.ConnectError):
                await api.get("https://down.example/")
            with pytest.raises(CircuitOpenError):
                await api.get("https://DOWN.example:443/x")  # the same origin
            assert (await api.get("https://up.example/")).status_code == 200
            assert (await api.get("http://down.example/")).status_code == 200
            assert (await api.get("https://down.example:8443/")).status_code == 200
        assert transport.state("https://down.example") == "open"
        assert transport.state("https://unknown.example") == "closed"

    async def test_turned_off(self) -> None:
        server = Server(error=True)
        transport = ResilientTransport(httpx.MockTransport(server), failure_threshold=None)
        async with httpx.AsyncClient(transport=transport) as api:
            for _ in range(10):
                with pytest.raises(httpx.ConnectError):
                    await api.get("https://api.example/")
        assert server.calls == 10
        assert transport.state("https://api.example") == "closed"
        server.error = False
        async with httpx.AsyncClient(transport=transport) as api:
            assert (await api.get("https://api.example/")).status_code == 200

    async def test_turned_off_and_cancelled(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")  # pragma: no cover

        transport = ResilientTransport(httpx.MockTransport(handler), failure_threshold=None)
        async with httpx.AsyncClient(transport=transport) as api:
            call = asyncio.create_task(api.get("https://api.example/"))
            await asyncio.sleep(0.01)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call

    async def test_a_bounded_number_of_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fastapi_alcazaba.outbound._MAX_ORIGINS", 2)
        transport = ResilientTransport(httpx.MockTransport(Server(error=True)), failure_threshold=1)
        async with httpx.AsyncClient(transport=transport) as api:
            for host in ("a", "b", "a", "c"):  # a was used after b: b is forgotten
                with pytest.raises(httpx.HTTPError):
                    await api.get(f"https://{host}.example/")
        assert [transport.state(f"https://{h}.example") for h in "abc"] == [
            "open",
            "closed",
            "open",
        ]


class Inner(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


class TestTransport:
    async def test_closing_it_closes_the_inner_one(self) -> None:
        inner = Inner()
        async with ResilientTransport(inner):
            pass
        assert inner.closed

    async def test_the_default_inner_transport(self) -> None:
        transport = ResilientTransport()
        assert isinstance(transport._transport, httpx.AsyncHTTPTransport)
        await transport.aclose()

    async def test_a_body_over_max_response_size_isnt_read_to_the_end(self) -> None:
        body = Trickle([b"x" * 4] * 100, 0)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=body)

        async with client(handler, max_response_size=10) as api:
            with pytest.raises(ResponseTooLargeError, match="api.example: response body larger"):
                await api.get("/")
        assert body.closed

    async def test_bodies_up_to_max_response_size(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=Trickle([b"x" * 5, b"x" * 5], 0))

        async with client(handler, max_response_size=10) as api:
            assert (await api.get("/")).text == "x" * 10

    async def test_max_response_size_counts_the_bytes_received_not_decoded(self) -> None:
        compressed = gzip.compress(b"x" * 1_000)

        async def handler(request: httpx.Request) -> httpx.Response:
            stream = Trickle([compressed], 0)
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

        async with client(handler, max_response_size=len(compressed)) as api:
            assert (await api.get("/")).text == "x" * 1_000

    def test_too_large_is_a_request_error_not_a_transport_error(self) -> None:
        """Like httpx.TooManyRedirects: retry policies on transport errors don't retry it."""
        assert issubclass(ResponseTooLargeError, httpx.RequestError)
        assert not issubclass(ResponseTooLargeError, httpx.TransportError)

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"timeout": 0}, "timeout"),
            ({"failure_threshold": 0}, "failure_threshold"),
            ({"reset_timeout": 0}, "reset_timeout"),
            ({"failure_statuses": [99]}, "failure_statuses"),
            ({"failure_statuses": [600]}, "failure_statuses"),
            ({"max_response_size": -1}, "max_response_size"),
        ],
    )
    def test_validation(self, options: dict[str, object], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            ResilientTransport(Inner(), **options)  # type: ignore[arg-type]
