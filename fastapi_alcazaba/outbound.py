"""Resilient outbound HTTP calls: a deadline for the whole call and a circuit breaker per origin.

    client = httpx.AsyncClient(transport=ResilientTransport(), base_url="https://api.example.com")

httpx's timeouts are per phase (connect, read, write, pool): a server that trickles a byte every
few seconds keeps a request alive forever, and a service that is down makes every caller wait for
its timeouts, request after request. This transport bounds the whole call, headers and body, and
stops calling an origin that keeps failing for a while: callers get `CircuitOpenError` at once,
and the service gets room to recover.

Calling URLs that users give (webhooks, link previews, avatars by URL) needs `SSRFGuardTransport`
as well, so that nobody makes the server call its own network:

    client = httpx.AsyncClient(transport=ResilientTransport(SSRFGuardTransport()))

Needs httpx2 (or httpx), which isn't a dependency of Alcazaba.
"""

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Collection, Iterable
from typing import cast

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef,import-not-found]

from fastapi_alcazaba.metrics import Metrics
from fastapi_alcazaba.resilience import CircuitBreaker
from fastapi_alcazaba.tracing import add_event

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
    if ":" in host:  # IPv6, bracketed as in URLs: http://[::1]:8080
        host = f"[{host}]"
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


class CircuitOpenError(httpx.TransportError):
    """The origin's circuit breaker is open: the request wasn't sent."""


class ResponseTooLargeError(httpx.RequestError):
    """The response body was larger than `max_response_size`: the rest wasn't read. A limit of the
    client, like `httpx.TooManyRedirects`, not a transport error: retrying won't help."""


def _metrics(metrics: Metrics | None) -> Metrics:
    if metrics is not None:
        return metrics
    from fastapi_alcazaba import FastAPICache

    return FastAPICache.get_metrics()


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
    :param metrics: where to count breakers opening and refusing calls
        (`fastapi_alcazaba.outbound.breaker`); FastAPICache's by default.
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
        metrics: Metrics | None = None,
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
        self._metrics = metrics
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
                _metrics(self._metrics).count(
                    "outbound.breaker", event="refused", **{"server.address": origin[1]}
                )
                add_event("fastapi_alcazaba.outbound.refused", **{"server.address": origin[1]})
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
            _metrics(self._metrics).count(
                "outbound.breaker", event="opened", **{"server.address": origin[1]}
            )


# region SSRF guard

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network
#: What `SSRFGuardTransport(allow=...)` takes: host names (`*.` for their subdomains), networks
#: and addresses.
AllowedDestination = str | _Network | _Address
#: What `SSRFGuardTransport` sends the requests with: a transport, or a function that makes one
#: for each host name.
InnerTransport = httpx.AsyncBaseTransport | Callable[[], httpx.AsyncBaseTransport]

_DEFAULT_PORTS = {"http": 80, "https": 443}
#: Besides what `ipaddress` doesn't call global: Python versions disagree on some of these, and
#: some are "global" for it without being anywhere a server should call.
_BLOCKED_V4 = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "0.0.0.0/8",  # "this network": 0.0.0.0 reaches the host itself
        "10.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT, also used inside clouds
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, with the cloud metadata services (169.254.169.254)
        "172.16.0.0/12",
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",
        "192.88.99.0/24",  # 6to4 relays, deprecated
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, and the broadcast address
    )
)
_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")  # IPv6 has no public addresses outside it
_BLOCKED_V6 = tuple(
    ipaddress.IPv6Network(network)
    for network in (
        "2001::/23",  # IETF protocol assignments, with Teredo (2001::/32), which tunnels to IPv4
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4, deprecated: relays forward it to the IPv4 address in it
        "3fff::/20",  # documentation (RFC 9637)
    )
)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # the IPv4 address it reaches is in its last bits
_HOST_NAME = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")


def _is_public(address: _Address) -> bool:
    """Whether `address` is on the public internet: not private, loopback, link-local,
    multicast, reserved, documentation..., nor an IPv6 form of such an IPv4 address."""
    if isinstance(address, ipaddress.IPv4Address):
        return (
            address.is_global
            and not address.is_multicast
            and not any(address in network for network in _BLOCKED_V4)
        )
    if address.ipv4_mapped is not None:
        return _is_public(address.ipv4_mapped)
    if address in _NAT64:
        return _is_public(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return (
        address in _GLOBAL_UNICAST
        and address.is_global
        and not any(address in network for network in _BLOCKED_V6)
    )


def _unwrap(address: _Address) -> _Address:
    """The IPv4 address an IPv4-mapped IPv6 address stands for; any other address as it is."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _literal(host: str) -> _Address | None:
    """The address `host` is, None for a name (IPv6 zones, `%25eth0`, are left out)."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _host_name(name: str) -> str:
    """A host name as httpx sends it (lowercase, IDNA) without its final dot."""
    host = ""
    if not any(char in name for char in "/@:?#[]% \\"):  # httpx would read a URL's host in it
        with contextlib.suppress(httpx.InvalidURL):
            host = httpx.URL(f"http://{name}/").raw_host.decode("ascii").rstrip(".")
    if not _HOST_NAME.fullmatch(host):
        raise ValueError(f"{name!r} isn't a host name, a network or an address")
    return host


def _proxied(transport: "httpx.AsyncBaseTransport") -> bool:
    """Whether it's an `httpx.AsyncHTTPTransport(proxy=...)`; other proxying transports can't be
    told apart."""
    pool = getattr(transport, "_pool", None)
    return type(pool).__name__ in ("AsyncHTTPProxy", "AsyncSOCKSProxy")


class BlockedDestinationError(httpx.RequestError):
    """`SSRFGuardTransport` didn't send the request: its scheme, port or address isn't allowed.
    A limit of the client, like `ResponseTooLargeError`: it isn't counted against the origin."""


class _Destination:
    """The transport of one host name, and how many of its responses are still open."""

    __slots__ = ("active", "transport")

    def __init__(self, transport: "httpx.AsyncBaseTransport") -> None:
        self.transport = transport
        self.active = 0


class _Released(httpx.AsyncByteStream):
    """A response body that tells its destination when it's closed."""

    def __init__(self, stream: "httpx.AsyncByteStream", destination: _Destination) -> None:
        self._stream = stream
        self._destination: _Destination | None = destination

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            if self._destination is not None:
                self._destination.active -= 1
                self._destination = None


class SSRFGuardTransport(httpx.AsyncBaseTransport):
    """An httpx transport that only calls the public internet: no private, loopback, link-local
    (cloud metadata), multicast or reserved addresses, whatever the URL says.

        client = httpx.AsyncClient(transport=ResilientTransport(SSRFGuardTransport()))

    It resolves the host itself and refuses the request (`BlockedDestinationError`) if any of its
    addresses isn't public, IPv6 forms of IPv4 addresses (`::ffff:127.0.0.1`, NAT64, 6to4) and
    numeric spellings (`2130706433`, `0x7f.1`) included. Then it connects to the address it
    checked: the URL is rewritten to it, the `Host` header keeps the name, and TLS gets the name
    for SNI and the certificate check. A DNS server that answers a public address to the check and
    a private one to the connection (DNS rebinding) gets nowhere. Each hop of a redirect comes
    through the transport again and is checked like the first.

    Wrap it in `ResilientTransport` rather than the other way round: the deadline then covers the
    DNS lookup, and each breaker is per host name. Give the transport to the client, not as one of
    its `mounts`, and give the client no `proxy`: those requests wouldn't go through it. A proxy
    in the inner transport is refused: it would connect on its own (an egress proxy that filters
    destinations, such as Smokescreen, is the alternative to this transport, not its companion).

    :param transport: what sends the requests. By default each host name gets an
        `httpx.AsyncHTTPTransport` of its own, sharing one SSL context: connections are pooled
        by address, and two names on the same address (a CDN, a shared host) must not share one,
        whose certificate was checked for the other name. Pass a function returning a transport
        (e.g. `lambda: httpx.AsyncHTTPTransport(http2=True)`) to make them yourself, or a
        transport (a mock in tests), to send everything through it.
    :param allow: host names (exact, or `*.example.com` for its subdomains), networks and
        addresses that may be reached although they aren't public, e.g. an internal service:
        `allow=["billing.internal", "10.1.2.0/24"]`. Requests to them are still pinned to the
        address checked.
    :param ports: the only ports allowed (`ports=(443,)`); None for any.
    :param max_hosts: host names with a transport of their own kept open at most (the least
        recently used ones without an open response are closed).
    :param metrics: where to count refused requests (`fastapi_alcazaba.outbound.ssrf.blocked`);
        FastAPICache's by default.
    """

    def __init__(
        self,
        transport: InnerTransport | None = None,
        *,
        allow: Iterable[AllowedDestination] = (),
        ports: Collection[int] | None = None,
        max_hosts: int = 100,
        metrics: Metrics | None = None,
    ) -> None:
        if ports is not None and not all(1 <= port <= 65_535 for port in ports):
            raise ValueError("ports must be between 1 and 65535 (or None for any)")
        if max_hosts < 1:
            raise ValueError("max_hosts must be at least 1")
        self._shared: httpx.AsyncBaseTransport | None = None
        self._factory: Callable[[], httpx.AsyncBaseTransport]
        if isinstance(transport, httpx.AsyncBaseTransport):
            if _proxied(transport):
                raise ValueError(_PROXY_REFUSED)
            self._shared = transport
        elif transport is not None:
            self._factory = transport
        else:
            context = httpx.create_ssl_context()
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=5)
            self._factory = lambda: httpx.AsyncHTTPTransport(verify=context, limits=limits)
        self.ports = frozenset(ports) if ports is not None else None
        self.max_hosts = max_hosts
        self._metrics = metrics
        self._hosts: set[str] = set()
        self._suffixes: list[str] = []
        self._networks: list[_Network] = []
        for entry in allow:
            if isinstance(entry, ipaddress.IPv4Address | ipaddress.IPv6Address):
                self._networks.append(ipaddress.ip_network(entry))
            elif isinstance(entry, ipaddress.IPv4Network | ipaddress.IPv6Network):
                self._networks.append(entry)
            else:
                try:
                    self._networks.append(ipaddress.ip_network(entry.strip(), strict=False))
                except ValueError:
                    name = entry.strip().lower()
                    if name.startswith("*."):
                        self._suffixes.append("." + _host_name(name[2:]))
                    else:
                        self._hosts.add(_host_name(name))
        self._destinations: OrderedDict[str, _Destination] = OrderedDict()

    async def check(self, url: "httpx.URL | str", *, timeout: float | None = 5.0) -> None:
        """Check `url` as a request to it would be, without sending anything: its scheme, its port
        and every address its host resolves to (within `timeout` seconds). Raises
        BlockedDestinationError when a request would be refused, and httpx.ConnectError when the
        host doesn't resolve. For a URL that users give (a webhook's endpoint), when they give it;
        every request is checked again all the same, as DNS answers change."""
        await self._check(httpx.Request("GET", url, extensions={"timeout": {"connect": timeout}}))

    async def handle_async_request(self, request: "httpx.Request") -> "httpx.Response":
        host, addresses = await self._check(request)
        name = _name(_origin(request.url))
        targets = list(dict.fromkeys(_unwrap(address) for address in addresses))
        first = targets[0]
        # The first address of the other family, if any: the network may lack IPv6 (or IPv4).
        other = next((target for target in targets if target.version != first.version), None)
        try:
            return await self._send(request, host, first)
        except httpx.ConnectError:
            if other is None:
                raise
            logger.debug("%s: couldn't connect to %s, trying %s", name, first, other)
        return await self._send(request, host, other)

    async def _check(self, request: "httpx.Request") -> tuple[str, list[_Address]]:
        """The host and its addresses, when they're allowed; else BlockedDestinationError."""
        url = request.url
        name = _name(_origin(url))
        if url.scheme not in _DEFAULT_PORTS:
            self._blocked("scheme")
            raise BlockedDestinationError(
                f"{url.scheme}: only http and https URLs are allowed", request=request
            )
        host = url.raw_host.decode("ascii")
        if not host:
            self._blocked("host")
            raise BlockedDestinationError("the URL has no host", request=request)
        port = url.port or _DEFAULT_PORTS[url.scheme]
        if self.ports is not None and port not in self.ports:
            self._blocked("port")
            raise BlockedDestinationError(f"{name}: port {port} isn't allowed", request=request)
        addresses = await self._resolve(request, host, port)
        trusted = self._allowed_name(host)
        for address in addresses:
            if not (trusted or self._allowed_address(address)):
                logger.warning("Blocked a request to %s, at %s", name, address)
                self._blocked("address")
                raise BlockedDestinationError(f"{name}: its address isn't allowed", request=request)
        return host, addresses

    def _blocked(self, reason: str) -> None:
        _metrics(self._metrics).count("outbound.ssrf.blocked", reason=reason)

    async def aclose(self) -> None:
        if self._shared is not None:
            await self._shared.aclose()
        destinations = list(self._destinations.values())
        self._destinations.clear()
        for destination in destinations:
            await destination.transport.aclose()

    async def _resolve(self, request: "httpx.Request", host: str, port: int) -> list[_Address]:
        literal = _literal(host)
        if literal is not None:
            return [literal]
        timeout = (request.extensions.get("timeout") or {}).get("connect")
        try:
            async with asyncio.timeout(timeout):
                found = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
        except TimeoutError as exc:
            raise httpx.ConnectTimeout(
                f"{host}: no answer from DNS within {timeout} s", request=request
            ) from exc
        except OSError as exc:  # socket.gaierror
            raise httpx.ConnectError(f"{host}: couldn't resolve it", request=request) from exc
        addresses = [_literal(str(info[4][0])) for info in found]
        unique = list(dict.fromkeys(address for address in addresses if address is not None))
        if not unique:
            raise httpx.ConnectError(f"{host}: couldn't resolve it", request=request)
        return unique

    def _allowed_name(self, host: str) -> bool:
        name = host.rstrip(".")
        return name in self._hosts or any(name.endswith(suffix) for suffix in self._suffixes)

    def _allowed_address(self, address: _Address) -> bool:
        unwrapped = _unwrap(address)
        return _is_public(address) or any(
            address in network or unwrapped in network for network in self._networks
        )

    async def _send(
        self,
        request: "httpx.Request",
        host: str,
        address: _Address,
    ) -> "httpx.Response":
        """Send `request` to `address`, with `host` for Host and TLS."""
        extensions = dict(request.extensions)
        if request.url.scheme == "https" and _literal(host) is None:
            extensions["sni_hostname"] = host
        headers = httpx.Headers(request.headers)
        if "host" not in headers:
            headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=str(address)),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        if self._shared is not None:
            return await self._shared.handle_async_request(pinned)
        destination, stale = self._acquire(host.rstrip("."))
        try:
            for evicted in stale:
                try:
                    await evicted.aclose()
                except Exception:
                    logger.warning("Couldn't close the transport of a host", exc_info=True)
            response = await destination.transport.handle_async_request(pinned)
        except BaseException:
            destination.active -= 1
            raise
        if response.is_closed:  # read already (made with content=): nothing stays open
            destination.active -= 1
        else:
            response.stream = _Released(cast(httpx.AsyncByteStream, response.stream), destination)
        return response

    def _acquire(self, host: str) -> tuple[_Destination, list["httpx.AsyncBaseTransport"]]:
        """The destination of `host`, counted as busy until its response is closed, and the
        transports of the hosts it pushes out, to close."""
        destination = self._destinations.get(host)
        if destination is None:
            transport = self._factory()
            if _proxied(transport):
                raise ValueError(_PROXY_REFUSED)  # not opened yet: nothing to close
            destination = self._destinations[host] = _Destination(transport)
        else:
            self._destinations.move_to_end(host)
        # Busy before anything awaits: another request can't close it meanwhile.
        destination.active += 1
        excess = len(self._destinations) - self.max_hosts
        if excess <= 0:
            return destination, []
        idle = [key for key, held in self._destinations.items() if held.active == 0][:excess]
        return destination, [self._destinations.pop(key).transport for key in idle]


_PROXY_REFUSED = (
    "SSRFGuardTransport can't pin addresses through a proxy, which connects on its own: filter "
    "destinations at the proxy instead"
)

# endregion
