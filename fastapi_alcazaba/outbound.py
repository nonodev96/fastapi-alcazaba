"""Resilient outbound HTTP calls: a deadline for the whole call and a circuit breaker per origin.

    client = httpx.AsyncClient(transport=ResilientTransport(), base_url="https://api.example.com")

httpx's timeouts are per phase (connect, read, write, pool): a server that trickles a byte every
few seconds keeps a request alive forever, and a service that is down makes every caller wait for
its timeouts, request after request. This transport bounds the whole call, headers and body, and
stops calling an origin that keeps failing for a while: callers get `CircuitOpenError` at once,
and the service gets room to recover.

Needs httpx2 (or httpx), which isn't a dependency of Alcazaba.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Collection

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef,import-not-found]

from fastapi_alcazaba.resilience import CircuitBreaker

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Origins with a breaker each transport remembers (the least recently used are forgotten), so an
#: app calling URLs it doesn't control (webhooks) can't grow it without end.
_MAX_ORIGINS = 1_000

_Origin = tuple[str, str, int | None]


def _origin(url: "httpx.URL") -> _Origin:
    # httpx lowercases the scheme and host and drops the scheme's default port.
    return url.scheme, url.host, url.port


def _name(origin: _Origin) -> str:
    scheme, host, port = origin
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


class CircuitOpenError(httpx.TransportError):
    """The origin's circuit breaker is open: the request wasn't sent."""


class ResponseTooLargeError(httpx.RequestError):
    """The response body was larger than `max_response_size`: the rest wasn't read. A limit of the
    client, like `httpx.TooManyRedirects`, not a transport error: retrying won't help."""


def _origin_failed(exc: BaseException) -> bool:
    """Whether the error is the origin's (it counts for its breaker): transport errors, timeouts
    included, but not the caller's own (an illegal header, a URL scheme httpx can't send)."""
    return isinstance(exc, httpx.TransportError) and not isinstance(
        exc, (httpx.LocalProtocolError, httpx.UnsupportedProtocol)
    )


class ResilientTransport(httpx.AsyncBaseTransport):
    """An httpx transport that gives every call a deadline and every origin a circuit breaker.

        client = httpx.AsyncClient(transport=ResilientTransport(timeout=5), base_url=...)

    Responses are read whole within the deadline before they're returned, so don't stream large
    downloads through it; `max_response_size` bounds what it reads.

    :param transport: the transport that sends the requests; `httpx.AsyncHTTPTransport()` by
        default (pass your own for its `verify`, `limits`, `proxy`...).
    :param timeout: seconds for the whole call, from sending the request to the last byte of the
        response; `httpx.TimeoutException` beyond. It applies to each request the transport
        sends: with `follow_redirects`, every hop gets a deadline of its own. None for no deadline
        (httpx's own timeouts still apply).
    :param failure_threshold: consecutive failures of an origin (scheme, host and port) after
        which it isn't called for `reset_timeout` seconds; then a single trial call decides
        whether it's back. Transport errors and timeouts count; errors of the caller's own
        (an illegal header value, an unsupported URL scheme, a request body that raises) don't.
        None disables the breaker.
    :param reset_timeout: seconds an open breaker refuses calls.
    :param failure_statuses: response statuses that count as failures, besides errors and timeouts.
        The response is still returned.
    :param max_response_size: bytes of response body read at most, as they come (before
        Content-Encoding is decoded); `ResponseTooLargeError` beyond, which isn't a failure of the
        origin. None for no limit.
    """

    def __init__(
        self,
        transport: "httpx.AsyncBaseTransport | None" = None,
        *,
        timeout: float | None = 10.0,
        failure_threshold: int | None = 5,
        reset_timeout: float = 30.0,
        failure_statuses: Collection[int] = (500, 502, 503, 504),
        max_response_size: int | None = None,
    ) -> None:
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive (or None for no deadline)")
        if failure_threshold is not None and failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1 (or None for no breaker)")
        if reset_timeout <= 0:
            raise ValueError("reset_timeout must be positive")
        if not all(100 <= status <= 599 for status in failure_statuses):
            raise ValueError("failure_statuses must be HTTP statuses (100-599)")
        if max_response_size is not None and max_response_size < 0:
            raise ValueError("max_response_size can't be negative (or None for no limit)")
        self._transport = transport if transport is not None else httpx.AsyncHTTPTransport()
        self.timeout = timeout
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_statuses = frozenset(failure_statuses)
        self.max_response_size = max_response_size
        self._breakers: OrderedDict[_Origin, CircuitBreaker] = OrderedDict()

    def state(self, url: "str | httpx.URL") -> str:
        """The breaker of `url`'s origin: "closed", "open" or "half-open" (a trial may go
        through)."""
        breaker = self._breakers.get(_origin(httpx.URL(url)))
        return "closed" if breaker is None else breaker.state

    async def handle_async_request(self, request: "httpx.Request") -> "httpx.Response":
        origin = _origin(request.url)
        breaker = self._breaker(origin)
        trial = False
        if breaker is not None:
            if (state := breaker.allow()) is None:
                raise CircuitOpenError(
                    f"{_name(origin)}: the circuit breaker is open", request=request
                )
            trial = state == "half-open"
        try:
            response = await self._send(request)
        except BaseException as exc:
            if _origin_failed(exc):
                self._failed(breaker, origin)
            elif trial and breaker is not None:
                breaker.abandon_trial()  # no verdict on the origin: the next call may try
            raise
        if response.status_code in self.failure_statuses:
            self._failed(breaker, origin)
        elif breaker is not None:
            breaker.record_success()
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def _send(self, request: "httpx.Request") -> "httpx.Response":
        try:
            async with asyncio.timeout(self.timeout):
                response = await self._transport.handle_async_request(request)
                if response.is_stream_consumed:
                    return response  # already in memory (a mock, a cache): nothing to read
                try:
                    body = await self._read(request, response)
                finally:
                    await response.aclose()
        except TimeoutError as exc:
            raise httpx.TimeoutException(
                f"{_name(_origin(request.url))}: no complete response within {self.timeout} s",
                request=request,
            ) from exc
        # The connection is closed by now: its stream is of no use to anyone.
        extensions = {k: v for k, v in response.extensions.items() if k != "network_stream"}
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(body),
            request=request,
            extensions=extensions,
        )

    async def _read(self, request: "httpx.Request", response: "httpx.Response") -> bytes:
        limit = self.max_response_size
        chunks: list[bytes] = []
        size = 0
        # Raw: the client decodes Content-Encoding itself, once.
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if limit is not None and size > limit:
                raise ResponseTooLargeError(
                    f"{_name(_origin(request.url))}: response body larger than {limit} bytes",
                    request=request,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _breaker(self, origin: _Origin) -> CircuitBreaker | None:
        if self.failure_threshold is None:
            return None
        breaker = self._breakers.get(origin)
        if breaker is not None:
            self._breakers.move_to_end(origin)
            return breaker
        breaker = CircuitBreaker(self.failure_threshold, self.reset_timeout)
        self._breakers[origin] = breaker
        if len(self._breakers) > _MAX_ORIGINS:
            self._breakers.popitem(last=False)
        return breaker

    def _failed(self, breaker: CircuitBreaker | None, origin: _Origin) -> None:
        if breaker is None:
            return
        before = breaker.state
        breaker.record_failure()
        if before != "open" and breaker.state == "open":
            logger.warning(
                "Circuit breaker of %s open: no calls for %s s", _name(origin), self.reset_timeout
            )
