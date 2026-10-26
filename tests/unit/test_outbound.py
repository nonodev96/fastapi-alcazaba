"""ResilientTransport: a deadline for the whole call and a circuit breaker per origin.
SSRFGuardTransport: only public addresses, pinned against DNS rebinding."""

import asyncio
import gzip
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx2 as httpx
import pytest

from fastapi_alcazaba.outbound import (
    BlockedDestinationError,
    CircuitOpenError,
    ResilientTransport,
    ResponseTooLargeError,
    SSRFGuardTransport,
    _is_public,
)

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


# region SSRF guard


class DNS:
    """Stands in for the event loop's getaddrinfo: answers from `records`, and counts."""

    def __init__(self) -> None:
        self.records: dict[str, list[list[str]]] = {}
        self.queries: list[tuple[str, int]] = []
        self.delay = 0.0

    def add(self, host: str, *answers: list[str]) -> None:
        """Each answer is a list of addresses; the last one repeats."""
        self.records[host] = list(answers)

    async def getaddrinfo(
        self, host: str, port: int, **options: object
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        assert options == {"type": socket.SOCK_STREAM}
        self.queries.append((host, port))
        await asyncio.sleep(self.delay)
        answers = self.records.get(host)
        if answers is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        addresses = answers.pop(0) if len(answers) > 1 else answers[0]
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))
            if ":" in address
            else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> DNS:
    dns = DNS()

    async def getaddrinfo(
        loop: object, host: str, port: int, **options: object
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        return await dns.getaddrinfo(host, port, **options)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return dns


class Origin:
    """A MockTransport handler that records what reaches the network, and answers `routes`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, httpx.Response] = {}
        self.unreachable: set[str] = set()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.unreachable:
            raise httpx.ConnectError("network unreachable", request=request)
        self.requests.append(request)
        return self.routes.get(request.url.path, httpx.Response(200, text="ok"))

    @property
    def sent(self) -> list[tuple[str, str, str | None]]:
        """(URL, Host header, SNI) of each request."""
        return [
            (str(r.url), r.headers["host"], r.extensions.get("sni_hostname")) for r in self.requests
        ]


@pytest.fixture
def origin() -> Origin:
    return Origin()


def guarded(origin: Origin, **options: Any) -> httpx.AsyncClient:
    transport = SSRFGuardTransport(httpx.MockTransport(origin), **options)
    return httpx.AsyncClient(transport=transport, follow_redirects=True)


PUBLIC = [
    "8.8.8.8",
    "93.184.216.34",
    "2606:4700:4700::1111",
    "2a00:1450:4001:80b::200e",
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
    "::ffff:8.8.8.8",
]
BLOCKED = [
    "127.0.0.1",
    "127.255.0.1",
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",  # cloud metadata
    "100.64.0.1",  # carrier-grade NAT
    "0.0.0.0",  # noqa: S104
    "0.1.2.3",
    "224.0.0.1",
    "239.255.255.250",
    "255.255.255.255",
    "240.0.0.1",
    "192.0.0.9",
    "192.0.2.1",
    "198.18.0.1",
    "198.51.100.1",
    "203.0.113.1",
    "192.88.99.1",
    "::",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::127.0.0.1",  # IPv4-compatible, deprecated
    "::ffff:0:127.0.0.1",  # IPv4-translated
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b:1::1",  # local-use NAT64
    "2002:808:808::1",  # 6to4
    "2002:7f00:1::1",
    "2001::1",  # Teredo
    "2001:db8::1",
    "3fff::1",
    "fc00::1",
    "fd00:ec2::254",  # AWS metadata over IPv6
    "fe80::1",
    "fec0::1",
    "ff02::1",
    "100::1",
    "2001:20::1",
]


class TestAddresses:
    @pytest.mark.parametrize("address", PUBLIC)
    def test_public(self, address: str) -> None:
        assert _is_public(ipaddress.ip_address(address))

    @pytest.mark.parametrize("address", BLOCKED)
    def test_not_public(self, address: str) -> None:
        assert not _is_public(ipaddress.ip_address(address))


class TestSSRFGuard:
    async def test_pinned_to_the_address_it_checked(self, dns: DNS, origin: Origin) -> None:
        dns.add("api.example", ["93.184.216.34"])
        async with guarded(origin) as client:
            response = await client.get("https://api.example:8443/hooks?x=1")
            await client.get("http://api.example/")
        assert response.text == "ok"
        assert str(response.url) == "https://api.example:8443/hooks?x=1"
        assert origin.sent == [
            ("https://93.184.216.34:8443/hooks?x=1", "api.example:8443", "api.example"),
            ("http://93.184.216.34/", "api.example", None),
        ]
        assert dns.queries == [("api.example", 8443), ("api.example", 80)]

    async def test_international_names(self, dns: DNS, origin: Origin) -> None:
        dns.add("xn--bcher-kva.example", ["93.184.216.34"])
        async with guarded(origin) as client:
            await client.get("https://bücher.example/")
        assert origin.sent == [
            ("https://93.184.216.34/", "xn--bcher-kva.example", "xn--bcher-kva.example")
        ]

    async def test_ipv6_addresses(self, dns: DNS, origin: Origin) -> None:
        dns.add("v6.example", ["2606:4700:4700::1111"])
        async with guarded(origin) as client:
            await client.get("https://v6.example/")
        assert origin.sent == [("https://[2606:4700:4700::1111]/", "v6.example", "v6.example")]

    async def test_ipv4_mapped_answers_connect_over_ipv4(self, dns: DNS, origin: Origin) -> None:
        dns.add("mapped.example", ["::ffff:8.8.8.8"])
        async with guarded(origin) as client:
            await client.get("http://mapped.example/")
        assert origin.sent == [("http://8.8.8.8/", "mapped.example", None)]

    @pytest.mark.parametrize("address", BLOCKED)
    async def test_names_with_blocked_addresses(
        self, dns: DNS, origin: Origin, caplog: pytest.LogCaptureFixture, address: str
    ) -> None:
        dns.add("evil.example", [address])
        async with guarded(origin) as client:
            with caplog.at_level(logging.WARNING, "fastapi_alcazaba.outbound"):
                with pytest.raises(BlockedDestinationError) as error:
                    await client.get("https://evil.example/latest/meta-data")
        assert str(error.value) == "https://evil.example: its address isn't allowed"
        # In the logs, not in the error, which may reach a user.
        assert str(ipaddress.ip_address(address)) in caplog.text
        assert origin.requests == []

    async def test_any_blocked_address_blocks(self, dns: DNS, origin: Origin) -> None:
        dns.add("mixed.example", ["93.184.216.34", "10.0.0.1"])
        async with guarded(origin) as client:
            with pytest.raises(BlockedDestinationError):
                await client.get("http://mixed.example/")
        assert origin.requests == []

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:8080/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:7f00:1]/",
            "http://[fe80::1%25eth0]/",
            "http://0.0.0.0:6379/",
        ],
    )
    async def test_literal_addresses(self, dns: DNS, origin: Origin, url: str) -> None:
        async with guarded(origin) as client:
            with pytest.raises(BlockedDestinationError):
                await client.get(url)
        assert (dns.queries, origin.requests) == ([], [])

    async def test_literal_public_addresses(self, dns: DNS, origin: Origin) -> None:
        async with guarded(origin) as client:
            await client.get("https://8.8.8.8/")
            await client.get("http://[::ffff:8.8.8.8]/")
        assert origin.sent == [
            ("https://8.8.8.8/", "8.8.8.8", None),
            ("http://8.8.8.8/", "[::ffff:8.8.8.8]", None),
        ]
        assert dns.queries == []

    @pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "127.1", "localhost", "localhost."])
    async def test_numeric_spellings_and_local_names(
        self, dns: DNS, origin: Origin, host: str
    ) -> None:
        """httpx sends them to DNS as names; the system resolver turns them into 127.0.0.1."""
        dns.add(host, ["127.0.0.1"])
        async with guarded(origin) as client:
            with pytest.raises(BlockedDestinationError):
                await client.get(f"http://{host}/")
        assert origin.requests == []

    async def test_dns_rebinding(self, dns: DNS, origin: Origin) -> None:
        """Public to the check, private to the connection: the connection uses the checked
        address, and every request resolves again."""
        dns.add("rebind.example", ["93.184.216.34"], ["127.0.0.1"])
        async with guarded(origin) as client:
            await client.get("http://rebind.example/")
            with pytest.raises(BlockedDestinationError):
                await client.get("http://rebind.example/")
        assert origin.sent == [("http://93.184.216.34/", "rebind.example", None)]

    async def test_every_redirect_is_checked(self, dns: DNS, origin: Origin) -> None:
        dns.add("public.example", ["93.184.216.34"])
        dns.add("internal.example", ["10.0.0.5"])
        origin.routes = {
            "/to-ip": httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"}),
            "/to-name": httpx.Response(307, headers={"Location": "http://internal.example/"}),
        }
        async with guarded(origin) as client:
            for path in ("/to-ip", "/to-name"):
                with pytest.raises(BlockedDestinationError):
                    await client.get(f"https://public.example{path}")
        assert [request.url.path for request in origin.requests] == ["/to-ip", "/to-name"]

    async def test_redirects_to_allowed_places(self, dns: DNS, origin: Origin) -> None:
        dns.add("public.example", ["93.184.216.34"])
        dns.add("other.example", ["8.8.8.8"])
        origin.routes = {
            "/relative": httpx.Response(301, headers={"Location": "/next"}),
            "/next": httpx.Response(302, headers={"Location": "https://other.example/end"}),
        }
        async with guarded(origin) as client:
            response = await client.get("https://public.example/relative")
        assert str(response.url) == "https://other.example/end"
        assert origin.sent == [
            ("https://93.184.216.34/relative", "public.example", "public.example"),
            ("https://93.184.216.34/next", "public.example", "public.example"),
            ("https://8.8.8.8/end", "other.example", "other.example"),
        ]

    async def test_only_http_and_https(self, origin: Origin) -> None:
        guard = SSRFGuardTransport(httpx.MockTransport(origin))
        with pytest.raises(BlockedDestinationError, match="ftp: only http and https"):
            await guard.handle_async_request(httpx.Request("GET", "ftp://files.example/"))

    async def test_a_url_without_host(self, origin: Origin) -> None:
        guard = SSRFGuardTransport(httpx.MockTransport(origin))
        with pytest.raises(BlockedDestinationError, match="no host"):
            await guard.handle_async_request(httpx.Request("GET", "http:///path"))

    async def test_ports(self, dns: DNS, origin: Origin) -> None:
        dns.add("api.example", ["93.184.216.34"])
        async with guarded(origin, ports=[443]) as client:
            await client.get("https://api.example/")
            for url in ("https://api.example:8443/", "http://api.example/"):
                with pytest.raises(BlockedDestinationError, match="port"):
                    await client.get(url)
        assert len(origin.requests) == 1

    async def test_requests_without_a_host_header(self, dns: DNS, origin: Origin) -> None:
        dns.add("api.example", ["93.184.216.34"])
        guard = SSRFGuardTransport(httpx.MockTransport(origin))
        request = httpx.Request("GET", "http://api.example:81/", stream=httpx.ByteStream(b""))
        assert "host" not in request.headers
        await guard.handle_async_request(request)
        assert origin.sent == [("http://93.184.216.34:81/", "api.example:81", None)]


class TestAllowed:
    async def test_names(self, dns: DNS, origin: Origin) -> None:
        dns.add("billing.internal", ["10.0.0.5"])
        dns.add("a.svc.cluster.local", ["10.0.0.6"])
        dns.add("svc.cluster.local", ["10.0.0.7"])
        async with guarded(origin, allow=["Billing.Internal.", "*.svc.cluster.local"]) as client:
            await client.get("https://billing.internal/")
            await client.get("http://a.svc.cluster.local/")
            with pytest.raises(BlockedDestinationError):
                await client.get("http://svc.cluster.local/")  # not a subdomain of itself
        assert origin.sent == [
            ("https://10.0.0.5/", "billing.internal", "billing.internal"),
            ("http://10.0.0.6/", "a.svc.cluster.local", None),
        ]

    async def test_networks_and_addresses(self, dns: DNS, origin: Origin) -> None:
        dns.add("in.example", ["10.1.2.3"])
        dns.add("mapped.example", ["::ffff:10.1.2.4"])
        dns.add("out.example", ["10.1.3.1"])
        allow = [
            "10.1.2.0/24",
            ipaddress.ip_address("192.168.0.10"),
            ipaddress.ip_network("fd00::/8"),
        ]
        async with guarded(origin, allow=allow) as client:
            await client.get("http://in.example/")
            await client.get("http://mapped.example/")
            await client.get("http://192.168.0.10/")
            await client.get("http://[fd00::1]/")
            for url in ("http://out.example/", "http://192.168.0.11/"):
                with pytest.raises(BlockedDestinationError):
                    await client.get(url)
        assert len(origin.requests) == 4

    @pytest.mark.parametrize(
        "entry", ["", "a/b", "http://x", "user@host", "*.", "a b", ".", "a..b", "<a>", "a\tb"]
    )
    def test_invalid_entries(self, entry: str) -> None:
        with pytest.raises(ValueError, match="isn't a host name"):
            SSRFGuardTransport(Inner(), allow=[entry])


class TestResolution:
    async def test_names_that_dont_resolve(self, dns: DNS, origin: Origin) -> None:
        dns.add("empty.example", [])
        async with guarded(origin) as client:
            for host in ("nowhere.example", "empty.example"):
                with pytest.raises(httpx.ConnectError, match="couldn't resolve it"):
                    await client.get(f"http://{host}/")

    async def test_a_slow_dns(self, dns: DNS, origin: Origin) -> None:
        dns.add("slow.example", ["93.184.216.34"])
        dns.delay = 1
        transport = SSRFGuardTransport(httpx.MockTransport(origin))
        timeout = httpx.Timeout(5, connect=0.01)
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            with pytest.raises(httpx.ConnectTimeout, match="no answer from DNS"):
                await client.get("http://slow.example/")

    async def test_the_other_family_when_the_first_cant_connect(
        self, dns: DNS, origin: Origin
    ) -> None:
        dns.add("dual.example", ["2606:4700:4700::1111", "2606:4700:4700::1001", "93.184.216.34"])
        origin.unreachable = {"2606:4700:4700::1111"}
        async with guarded(origin) as client:
            assert (await client.get("http://dual.example/")).text == "ok"
        assert origin.sent == [("http://93.184.216.34/", "dual.example", None)]

    async def test_no_other_family(self, dns: DNS, origin: Origin) -> None:
        dns.add("v6.example", ["2606:4700:4700::1111", "2606:4700:4700::1001"])
        origin.unreachable = {"2606:4700:4700::1111"}
        async with guarded(origin) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://v6.example/")

    async def test_neither_family(self, dns: DNS, origin: Origin) -> None:
        dns.add("dual.example", ["2606:4700:4700::1111", "93.184.216.34"])
        origin.unreachable = {"2606:4700:4700::1111", "93.184.216.34"}
        async with guarded(origin) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("http://dual.example/")


class TestWithResilientTransport:
    def test_blocked_is_a_request_error_not_a_transport_error(self) -> None:
        assert issubclass(BlockedDestinationError, httpx.RequestError)
        assert not issubclass(BlockedDestinationError, httpx.TransportError)

    async def test_blocked_requests_dont_open_the_breaker(self, dns: DNS, origin: Origin) -> None:
        dns.add("evil.example", ["127.0.0.1"])
        transport = ResilientTransport(
            SSRFGuardTransport(httpx.MockTransport(origin)), failure_threshold=1
        )
        async with httpx.AsyncClient(transport=transport) as client:
            for _ in range(3):
                with pytest.raises(BlockedDestinationError):
                    await client.get("http://evil.example/")
        assert transport.state("http://evil.example") == "closed"

    async def test_the_breaker_is_per_name(self, dns: DNS, origin: Origin) -> None:
        dns.add("down.example", ["93.184.216.34"])
        dns.add("up.example", ["93.184.216.34"])
        origin.routes = {"/fail": httpx.Response(503)}
        transport = ResilientTransport(
            SSRFGuardTransport(httpx.MockTransport(origin)), failure_threshold=1
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("http://down.example/fail")
            assert (await client.get("http://up.example/")).text == "ok"
        assert transport.state("http://down.example") == "open"


class Tracked(httpx.AsyncBaseTransport):
    """A transport per host name, that knows whether it was closed."""

    def __init__(self, origin: Origin, made: list["Tracked"], *, close_fails: bool = False) -> None:
        self.inner = httpx.MockTransport(origin)
        self.closed = False
        self.close_fails = close_fails
        made.append(self)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stream":
            return httpx.Response(200, stream=Trickle([b"a", b"b"], 0))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        self.closed = True
        if self.close_fails:
            raise OSError("can't close")


class TestTransportPerName:
    async def test_names_on_the_same_address_dont_share_connections(
        self, dns: DNS, origin: Origin
    ) -> None:
        for host in ("a.example", "b.example", "a.example."):
            dns.add(host, ["93.184.216.34"])
        made: list[Tracked] = []
        transport = SSRFGuardTransport(lambda: Tracked(origin, made))
        async with httpx.AsyncClient(transport=transport) as client:
            for host in ("a.example", "b.example", "a.example", "a.example."):
                await client.get(f"https://{host}/")
        assert len(made) == 2
        assert all(tracked.closed for tracked in made)

    async def test_least_recently_used_names_are_closed(self, dns: DNS, origin: Origin) -> None:
        for host in "abc":
            dns.add(f"{host}.example", ["93.184.216.34"])
        made: list[Tracked] = []
        transport = SSRFGuardTransport(lambda: Tracked(origin, made), max_hosts=2)
        async with httpx.AsyncClient(transport=transport) as client:
            for host in "abac":  # a was used after b: b goes
                await client.get(f"http://{host}.example/")
            assert [tracked.closed for tracked in made] == [False, True, False]
            await client.get("http://b.example/")
        assert len(made) == 4

    async def test_names_with_an_open_response_stay(self, dns: DNS, origin: Origin) -> None:
        for host in "abc":
            dns.add(f"{host}.example", ["93.184.216.34"])
        made: list[Tracked] = []
        transport = SSRFGuardTransport(lambda: Tracked(origin, made), max_hosts=1)
        async with httpx.AsyncClient(transport=transport) as client:
            async with client.stream("GET", "http://a.example/stream") as streaming:
                await client.get("http://b.example/")  # a is busy: one over the limit for now
                assert [tracked.closed for tracked in made] == [False, False]
                assert await streaming.aread() == b"ab"
            await client.get("http://c.example/")
            assert [tracked.closed for tracked in made] == [True, True, False]

    async def test_a_transport_that_fails_to_close(
        self, dns: DNS, origin: Origin, caplog: pytest.LogCaptureFixture
    ) -> None:
        for host in "ab":
            dns.add(f"{host}.example", ["93.184.216.34"])
        made: list[Tracked] = []
        transport = SSRFGuardTransport(lambda: Tracked(origin, made, close_fails=True), max_hosts=1)
        client = httpx.AsyncClient(transport=transport)
        await client.get("http://a.example/")
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.outbound"):
            assert (await client.get("http://b.example/")).text == "ok"
        assert "Couldn't close the transport" in caplog.text

    async def test_a_failed_request_frees_its_name(self, dns: DNS, origin: Origin) -> None:
        for host in "ab":
            dns.add(f"{host}.example", ["93.184.216.34"])
        made: list[Tracked] = []
        origin.unreachable = {"93.184.216.34"}
        transport = SSRFGuardTransport(lambda: Tracked(origin, made), max_hosts=1)
        async with httpx.AsyncClient(transport=transport) as client:
            for host in "ab":
                with pytest.raises(httpx.ConnectError):
                    await client.get(f"http://{host}.example/")
            assert [tracked.closed for tracked in made] == [True, False]

    async def test_responses_are_released_once(self, dns: DNS, origin: Origin) -> None:
        dns.add("a.example", ["93.184.216.34"])
        transport = SSRFGuardTransport(lambda: Tracked(origin, []))
        request = httpx.Request("GET", "http://a.example/stream")
        response = await transport.handle_async_request(request)
        assert transport._destinations["a.example"].active == 1
        assert isinstance(response.stream, httpx.AsyncByteStream)
        await response.stream.aclose()
        await response.stream.aclose()
        assert transport._destinations["a.example"].active == 0

    async def test_the_default_transports(self) -> None:
        guard = SSRFGuardTransport()
        made = guard._factory()
        assert isinstance(made, httpx.AsyncHTTPTransport)
        await made.aclose()
        await guard.aclose()

    async def test_closing_a_shared_transport(self) -> None:
        inner = Inner()
        async with SSRFGuardTransport(inner):
            pass
        assert inner.closed


class TestProxies:
    def test_a_proxied_transport_is_refused(self) -> None:
        with pytest.raises(ValueError, match="proxy"):
            SSRFGuardTransport(httpx.AsyncHTTPTransport(proxy="http://proxy.internal:3128"))

    async def test_a_factory_of_proxied_transports_is_refused(self, dns: DNS) -> None:
        dns.add("a.example", ["93.184.216.34"])
        guard = SSRFGuardTransport(lambda: httpx.AsyncHTTPTransport(proxy="http://proxy:3128"))
        with pytest.raises(ValueError, match="proxy"):
            await guard.handle_async_request(httpx.Request("GET", "http://a.example/"))


class TestGuardValidation:
    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"ports": [0]}, "ports"),
            ({"ports": [65_536]}, "ports"),
            ({"max_hosts": 0}, "max_hosts"),
        ],
    )
    def test_validation(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            SSRFGuardTransport(Inner(), **options)


# endregion
