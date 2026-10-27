"""The cache itself: configuration, backend and helpers. FastAPICache is a global instance of it."""

import hashlib
import warnings
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from starlette.requests import HTTPConnection

from fastapi_alcazaba.coder import Coder, JsonCoder
from fastapi_alcazaba.compression import MAX_DECOMPRESSED_SIZE, Codec, Compressor
from fastapi_alcazaba.errors import UnsignedEntriesWarning
from fastapi_alcazaba.integrity import EntryProtector, Secrets, Signer
from fastapi_alcazaba.key_builder import default_key_builder
from fastapi_alcazaba.metrics import Metrics
from fastapi_alcazaba.resilience import BackendGuard, CircuitBreaker
from fastapi_alcazaba.tracing import Tracer
from fastapi_alcazaba.types import Backend, KeyBuilder

if TYPE_CHECKING:
    from fastapi_alcazaba.decorator import Tags, _CacheDecorator
    from fastapi_alcazaba.locks import Lock

T = TypeVar("T")

#: Request headers that make each user's response different: by default each value gets its own
#: cache entry, and the response says `Cache-Control: private` so shared proxies don't keep it.
DEFAULT_VARY_HEADERS: tuple[str, ...] = ("authorization", "cookie")


def normalise_headers(headers: Iterable[str]) -> tuple[str, ...]:
    """Lowercase header names without duplicates, in their original order."""
    return tuple(dict.fromkeys(header.strip().lower() for header in headers if header.strip()))


def warn_unsigned(coder: type[Coder]) -> None:
    warnings.warn(
        f"{coder.__name__} can run code when it reads an entry that someone else wrote to the "
        "backend; pass signing_key=... or encryption_key=... so that Alcazaba only reads entries "
        "it wrote itself",
        UnsignedEntriesWarning,
        stacklevel=4,
    )


@dataclass(frozen=True)
class CacheOptions:
    """How a cache protects, varies, locks, refreshes and compresses its entries."""

    #: Signs or encrypts the entries (signing_key / encryption_key); None stores them as they are.
    protector: EntryProtector | None = None
    vary_headers: tuple[str, ...] = DEFAULT_VARY_HEADERS
    respect_cache_control: bool = False
    single_flight: bool = True
    max_value_size: int | None = None
    distributed_lock: bool = False
    lock_timeout: float = 10.0
    lock_wait: float = 5.0
    early_refresh: float | None = None
    #: Compresses large entries (compression=...); None stores them as they are.
    compressor: Compressor | None = None

    @property
    def decompression_limit(self) -> int:
        return self.max_value_size or MAX_DECOMPRESSED_SIZE

    def __post_init__(self) -> None:
        if self.max_value_size is not None and self.max_value_size < 1:
            raise ValueError("max_value_size must be at least 1 byte (or None for no limit)")
        if self.lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive")
        if self.lock_wait < 0:
            raise ValueError("lock_wait can't be negative")
        if self.early_refresh is not None and not 0 < self.early_refresh <= 1:
            raise ValueError("early_refresh is a fraction of the lifetime, between 0 and 1")


class Cache:
    """A cache: a backend and its settings. Create one per app, or several (e.g. a public one and
    an encrypted one for personal data), and decorate with `cached()`; or use the global
    FastAPICache with `@cache`.

    :param backend: where entries are stored.
    :param prefix: prefix of every key.
    :param expire: default time to live, in seconds.
    :param coder: how values are stored (JsonCoder by default).
    :param key_builder: builds keys from the function and its arguments.
    :param cache_status_header: response header saying HIT or MISS; "" to leave it out.
    :param enable: False turns the cache off.
    :param signing_key: secret (at least 32 bytes; text, bytes, or pydantic's SecretStr or
        SecretBytes) used to sign every entry with HMAC-SHA256; entries without a valid signature
        are ignored. Strongly recommended when the backend is
        shared or reachable by others, and needed for PickleCoder. A list of keys rotates them:
        the first one signs, all of them are accepted.
    :param encryption_key: like `signing_key`, but the entries are also encrypted (AES-256-GCM):
        a dump or a backup of the backend doesn't reveal the cached responses. Needs the `crypto`
        extra. Use one or the other, not both.
    :param vary_headers: request headers that give each value its own cache entry and make the
        response `private` (see DEFAULT_VARY_HEADERS). Pass `()` for endpoints whose response is
        the same for everybody.
    :param respect_cache_control: let clients skip the cache with `Cache-Control: no-cache` or
        `no-store`. Off by default: anyone could otherwise make every request reach the endpoint,
        which defeats the cache as a shield against load.
    :param single_flight: concurrent misses for the same key wait for the first one instead of
        all computing the value (cache stampede protection), within each process.
    :param backend_timeout: seconds each backend call may take before the cache is skipped for
        that request; None waits as long as the client does.
    :param circuit_breaker_failures: consecutive backend failures after which the backend is not
        called for `circuit_breaker_reset` seconds (requests run uncached and don't wait for
        it); None disables the breaker.
    :param max_value_size: values larger than this many bytes (once encoded) aren't stored.
    :param distributed_lock: on a miss, take a lock in the backend so that a single process
        computes the value while the others wait for it (up to `lock_wait` seconds, then they
        compute it themselves). The lock expires after `lock_timeout` seconds.
    :param early_refresh: fraction of the lifetime at its end in which a hit may recompute the
        value (more likely as the expiry nears), so that popular entries are refreshed before
        they expire instead of all callers missing at once. E.g. 0.1.
    :param compression: "zstd" (Python 3.14, or the `zstd` extra) or "zlib": entries of at least
        `compression_min_size` bytes are stored compressed when that makes them smaller, with
        `compression_level` (the codec's default if None). Compressed entries are read back
        whatever this setting, so it can be turned on and off freely.
    :param metrics: record OpenTelemetry metrics (needs the `otel` extra).
    :param meter_provider: OpenTelemetry meter provider; the global one by default.
    :param tracing: record OpenTelemetry spans of cached calls and backend calls (`otel` extra).
    :param tracer_provider: OpenTelemetry tracer provider; the global one by default.
    """

    def __init__(
        self,
        backend: Backend,
        prefix: str = "",
        expire: int | None = None,
        coder: type[Coder] = JsonCoder,
        key_builder: KeyBuilder = default_key_builder,
        cache_status_header: str = "X-FastAPI-Cache",
        enable: bool = True,
        *,
        signing_key: Secrets | None = None,
        encryption_key: Secrets | None = None,
        vary_headers: Iterable[str] = DEFAULT_VARY_HEADERS,
        respect_cache_control: bool = False,
        single_flight: bool = True,
        backend_timeout: float | None = 1.0,
        circuit_breaker_failures: int | None = 5,
        circuit_breaker_reset: float = 30.0,
        max_value_size: int | None = None,
        distributed_lock: bool = False,
        lock_timeout: float = 10.0,
        lock_wait: float = 5.0,
        early_refresh: float | None = None,
        compression: Codec | None = None,
        compression_min_size: int = 1024,
        compression_level: int | None = None,
        metrics: bool = True,
        meter_provider: Any = None,
        tracing: bool = True,
        tracer_provider: Any = None,
    ) -> None:
        from fastapi_alcazaba import __version__

        protector: EntryProtector | None = None
        if signing_key is not None and encryption_key is not None:
            raise ValueError("encryption_key already authenticates the entries: pass only one key")
        if encryption_key is not None:
            from fastapi_alcazaba.encryption import Encrypter

            protector = Encrypter(encryption_key)
        elif signing_key is not None:
            protector = Signer(signing_key)
        self.options = CacheOptions(
            protector=protector,
            vary_headers=normalise_headers(vary_headers),
            respect_cache_control=respect_cache_control,
            single_flight=single_flight,
            max_value_size=max_value_size,
            distributed_lock=distributed_lock,
            lock_timeout=lock_timeout,
            lock_wait=lock_wait,
            early_refresh=early_refresh,
            compressor=(
                Compressor(compression, min_size=compression_min_size, level=compression_level)
                if compression is not None
                else None
            ),
        )
        self.metrics = Metrics(enabled=metrics, meter_provider=meter_provider, version=__version__)
        breaker = (
            CircuitBreaker(circuit_breaker_failures, circuit_breaker_reset)
            if circuit_breaker_failures is not None
            else None
        )
        self.tracer = Tracer(enabled=tracing, tracer_provider=tracer_provider, version=__version__)
        self.guard = BackendGuard(backend_timeout, breaker, self.metrics, self.tracer)
        if coder.requires_signing and protector is None:
            warn_unsigned(coder)
        if distributed_lock and not backend.supports_locks:
            warnings.warn(
                f"{type(backend).__name__} can't hold locks: distributed_lock does nothing",
                stacklevel=3,
            )
        self.backend = backend
        self.prefix = prefix
        self.expire = expire
        self.coder = coder
        self.key_builder = key_builder
        self.cache_status_header = cache_status_header
        self.enable = enable

    def cached(
        self,
        expire: int | None = None,
        coder: type[Coder] | None = None,
        key_builder: KeyBuilder | None = None,
        namespace: str = "",
        injected_dependency_namespace: str = "__fastapi_alcazaba",
        **options: Any,
    ) -> "_CacheDecorator":
        """Like `fastapi_alcazaba.decorator.cache`, with this cache instead of the global
        FastAPICache."""
        from fastapi_alcazaba.decorator import cache

        return cache(
            expire,
            coder,
            key_builder,
            namespace,
            injected_dependency_namespace,
            instance=self,
            **options,
        )

    async def get_or_set(
        self,
        key: str,
        compute: Callable[[], Awaitable[T]],
        *,
        expire: int | None = None,
        namespace: str = "",
        tags: "Tags | None" = None,
    ) -> T:
        """The value cached under `key`, or else `await compute()`, stored. For caching inside an
        endpoint what doesn't fit a decorator. Same pipeline as `cached()` (signing, encryption,
        compression, single flight, locks); annotate `compute`'s return type to get it back as
        that type. The key is hashed: user input in it never reaches the backend in clear."""
        digest = hashlib.sha256(key.encode()).hexdigest()

        def key_builder(_func: Any, namespace: str = "", **_kwargs: Any) -> str:
            return f"{namespace}:{digest}"

        decorated = self.cached(
            expire=expire, namespace=namespace, key_builder=key_builder, vary_headers=(), tags=tags
        )(compute)
        return await decorated()  # type: ignore[return-value]  # no request: never a Response

    def state_key(self, kind: str, rest: str) -> str:
        """Backend key of state other than cached responses: locks, rate-limit counters, revoked
        tokens, one-time codes, idempotency records. It goes under `{prefix}!`, not `{prefix}:`,
        so that `clear()` never frees a held lock, resets a rate limit or takes a revocation
        back, and InMemoryBackend keeps it apart from the entries it evicts."""
        return f"{self.prefix}!{kind}:{rest}"

    def tag_key(self, tag: str) -> str:
        """Backend key of a tag. The digest goes in {braces} (a cluster hash tag) so that the set
        and its temporary copy share a slot, whatever characters the tag has."""
        digest = hashlib.sha256(tag.encode()).hexdigest()[:32]
        return f"{self.prefix}:tag:{{{digest}}}"

    async def invalidate_tags(self, *tags: str) -> int:
        """Delete every entry stored with any of `tags` (see `cached(tags=...)`)."""
        if not self.backend.supports_tags:
            raise NotImplementedError(f"{type(self.backend).__name__} can't invalidate by tag")
        return await self.backend.invalidate_tags([self.tag_key(tag) for tag in tags])

    def lock(
        self,
        name: str,
        *,
        ttl: float = 30.0,
        wait: float | None = 0.0,
        renew: bool = True,
    ) -> "Lock":
        """A lock shared by every process using this backend (see `fastapi_alcazaba.locks`):

            async with cache.lock("nightly-report", ttl=60):
                ...

        `ttl`, `wait` and `renew` are those of `Lock`.
        """
        from fastapi_alcazaba.locks import Lock

        return Lock(self, name, ttl=ttl, wait=wait, renew=renew)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        """Delete the entry `key` (a full cache key), or every entry of `namespace`, or of the
        whole prefix when neither is given. Cached responses only: locks, counters, revoked tokens
        and the like are kept apart (`state_key`)."""
        if key is not None and namespace is None:
            # Not with the prefix as the namespace: that would clear the whole prefix.
            return await self.backend.clear(key=key)
        full_namespace = self.prefix + (":" + namespace if namespace else "")
        return await self.backend.clear(full_namespace, key)

    async def close(self) -> None:
        """Close the backend if it holds resources (TieredBackend's listener, DynamoDB client)."""
        close = getattr(self.backend, "close", None)
        if close is not None:
            await close()

    def install(self, app: Any) -> "Cache":
        """Make this cache the one `get_cache` returns for `app`."""
        app.state.fastapi_alcazaba = self
        return self


def get_cache(request: HTTPConnection) -> Cache:
    """FastAPI dependency: the cache installed on the app (`Cache.install`), else FastAPICache's.
    Works in WebSocket routes too.

        @app.post("/products/{product_id}")
        async def update(product_id: int, cache: Annotated[Cache, Depends(get_cache)]) -> None:
            await cache.invalidate_tags(f"product:{product_id}")
    """
    installed = getattr(request.app.state, "fastapi_alcazaba", None)
    if isinstance(installed, Cache):
        return installed
    from fastapi_alcazaba import FastAPICache

    return FastAPICache.instance()


def metrics_of(connection: HTTPConnection) -> Metrics:
    """The metrics of the cache `get_cache` gives, disabled when there's none: for the tools that
    record metrics without needing a cache (middlewares, error handlers)."""
    try:
        return get_cache(connection).metrics
    except (RuntimeError, KeyError):  # no cache configured, or a bare ASGI app without "app"
        return Metrics(enabled=False)
