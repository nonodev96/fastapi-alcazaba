import asyncio
import functools
import hashlib
import logging
import random
import secrets
import warnings
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from functools import wraps
from inspect import Parameter, Signature, isawaitable, iscoroutinefunction
from types import BuiltinFunctionType, FunctionType, MethodType
from typing import Any, ParamSpec, Protocol, TypeVar, cast, overload

from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.utils import get_typed_return_annotation, get_typed_signature
from starlette.requests import Request
from starlette.responses import Response
from starlette.status import HTTP_200_OK, HTTP_304_NOT_MODIFIED

from fastapi_alcazaba.backends.base import Backend
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen, UnstableCacheKeyWarning
from fastapi_alcazaba.core.integrity import EntryProtector
from fastapi_alcazaba.core.resilience import BackendGuard
from fastapi_alcazaba.pagination.paginators import CursorParams, LimitOffsetParams, PageParams
from fastapi_alcazaba.response_cache.caches import (
    Cache,
    CacheOptions,
    FastAPICache,
    normalise_headers,
    warn_unsigned,
)
from fastapi_alcazaba.response_cache.coder import Coder, type_adapter_for
from fastapi_alcazaba.response_cache.compression import decompress
from fastapi_alcazaba.response_cache.key_builder import KeyBuilder, default_key_builder

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
P = ParamSpec("P")
R = TypeVar("R")

# Misses being computed, by event loop and cache key (see `single_flight`).
_in_flight: dict[tuple[int, str], "asyncio.Future[bytes | None]"] = {}
_warned_coders: set[type[Coder]] = set()
_UNUSABLE = object()

Tags = Iterable[str] | Callable[..., Iterable[str]]


def _augment_signature(signature: Signature, *extra: Parameter) -> Signature:
    if not extra:
        return signature

    parameters = list(signature.parameters.values())
    variadic_keyword_params: list[Parameter] = []
    while parameters and parameters[-1].kind is Parameter.VAR_KEYWORD:
        variadic_keyword_params.append(parameters.pop())

    return signature.replace(parameters=[*parameters, *extra, *variadic_keyword_params])


def _locate_param(sig: Signature, dep: Parameter, to_inject: list[Parameter]) -> Parameter:
    """Locate an existing parameter in the decorated endpoint

    If not found, returns the injectable parameter, and adds it to the to_inject list.

    """
    param = next((p for p in sig.parameters.values() if p.annotation is dep.annotation), None)
    if param is None:
        to_inject.append(dep)
        param = dep
    return param


def _check_excluded_params(sig: Signature, exclude_params: Collection[str]) -> None:
    """A typo in exclude_params fails when the function is decorated.
    Functions taking **kwargs accept any name."""
    if any(p.kind is Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return
    unknown = sorted(set(exclude_params) - set(sig.parameters))
    if unknown:
        raise ValueError(
            "exclude_params contains parameters not present in the signature: " + ", ".join(unknown)
        )


def _positional_names(sig: Signature) -> list[str]:
    return [
        p.name
        for p in sig.parameters.values()
        if p.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
    ]


def _exclude_params(
    sig: Signature,
    exclude_params: Collection[str],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Drop the named parameters from the arguments used to build the cache key; positional
    arguments are matched to their names by position."""
    filtered_kwargs = {k: v for k, v in kwargs.items() if k not in exclude_params}
    if not args:
        return args, filtered_kwargs
    positional = _positional_names(sig)
    filtered_args = tuple(
        value
        for i, value in enumerate(args)
        if i >= len(positional) or positional[i] not in exclude_params
    )
    return filtered_args, filtered_kwargs


def _has_unstable_repr(value: object) -> bool:
    """True when repr() shows a memory address: the default object repr, functions, methods."""
    return type(value).__repr__ is object.__repr__ or isinstance(
        value, FunctionType | MethodType | BuiltinFunctionType | functools.partial
    )


def _warn_unstable_key(
    func: Callable[..., Any],
    sig: Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    positional = _positional_names(sig)
    names = [
        positional[i] if i < len(positional) else f"argument {i}"
        for i, value in enumerate(args)
        if _has_unstable_repr(value)
    ] + [name for name, value in kwargs.items() if _has_unstable_repr(value)]
    if not names:
        return
    message = (
        f"{func.__module__}.{func.__qualname__}: the repr() of {', '.join(names)} changes with "
        "every call (it shows a memory address), so the cache key never repeats and the cache "
        f"never hits. Leave them out with @cache(exclude_params={names!r})"
    )
    logger.warning(message)
    warnings.warn(message, UnstableCacheKeyWarning, stacklevel=4)


def _cache_control_directives(request: Request | None) -> frozenset[str]:
    """Directive names of a Cache-Control header: "no-cache, max-age=0" -> {"no-cache",
    "max-age"}, so that "no-cache, max-age=0" counts as no-cache too."""
    if request is None:
        return frozenset()
    header = request.headers.get("cache-control", "")
    return frozenset(
        part.split("=", 1)[0].strip().lower() for part in header.split(",") if part.strip()
    )


def _compute_etag(payload: bytes) -> str:
    """Weak ETag of the stored bytes, quoted, the same in every process and after restarts (not
    hash(), which changes with each process)."""
    return f'W/"{hashlib.sha256(payload).hexdigest()[:32]}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """If-None-Match is "*" or a list of tags, compared weakly (RFC 9110, section 13.1.2)."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    opaque = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == opaque for tag in if_none_match.split(","))


def _vary_digest(request: Request | None, headers: tuple[str, ...]) -> str | None:
    """Digest of the request's values of `headers`; None when it carries none of them."""
    if request is None or not headers:
        return None
    values = [request.headers.get(name) for name in headers]
    if all(value is None for value in values):
        return None
    digest = hashlib.sha256()
    for name, value in zip(headers, values, strict=True):
        digest.update(name.encode() + b"\0")
        # Starlette decodes header values as latin-1, so this gives back the original bytes.
        digest.update(b"\1" if value is None else b"\2" + value.encode("latin-1"))
        digest.update(b"\0")
    return digest.hexdigest()


def _forbids_storing(target: Response | None) -> bool:
    if target is None:
        return False
    directives = target.headers.get("cache-control", "").lower()
    return "no-store" in directives


def _is_cacheable(result: object, response: Response | None) -> bool:
    """Only successful responses that don't say no-store. A Response returned with another
    status, or a status set on the injected response, would come back from the cache as a 200
    with the error body."""
    if isinstance(result, Response) and (
        result.status_code != HTTP_200_OK or _forbids_storing(result)
    ):
        return False
    status_code: Any = getattr(response, "status_code", None)  # FastAPI leaves it None
    return status_code in (None, HTTP_200_OK) and not _forbids_storing(response)


def _add_cache_status(target: Response, name: str | None, status: str) -> None:
    """This cache's member of the Cache-Status header (RFC 9211): `name; hit; ttl=60`, or why
    the request was forwarded, `name; fwd=miss; stored`. The list goes from the origin to the
    client: after the members the endpoint added (caches of its own), before those of the caches
    the response goes through on its way (a CDN)."""
    if not name:
        return
    member = f"{name}; {status}"
    existing = target.headers.get("cache-status")
    target.headers["Cache-Status"] = f"{existing}, {member}" if existing else member


def _hit(ttl: int | None) -> str:
    return f"hit; ttl={ttl}" if ttl is not None and ttl > 0 else "hit"


@dataclass(frozen=True)
class _HeaderPolicy:
    status_name: str | None
    vary: tuple[str, ...]
    private: bool
    stale_while_revalidate: int | None

    def cache_control(self, max_age: int | None) -> str | None:
        directives = ["private"] if self.private else []
        if max_age is not None and max_age > 0:
            directives.append(f"max-age={max_age}")
            if self.stale_while_revalidate:
                directives.append(f"stale-while-revalidate={self.stale_while_revalidate}")
        return ", ".join(directives) or None

    def apply(
        self,
        targets: Iterable[Response],
        status: str,
        etag: str,
        max_age: int | None,
    ) -> None:
        cache_control = self.cache_control(max_age)
        for target in targets:
            target.headers["ETag"] = etag
            _add_cache_status(target, self.status_name, status)
            if cache_control:
                target.headers["Cache-Control"] = cache_control
            if self.vary:
                existing = [
                    v.strip() for v in target.headers.get("vary", "").split(",") if v.strip()
                ]
                seen = {v.lower() for v in existing}
                added = [name.title() for name in self.vary if name not in seen]
                target.headers["Vary"] = ", ".join(existing + added)


def _add_page_links(result: object, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """A page served from the cache gets the Link header that `paginate` gives it when the
    endpoint runs. The links are made from this request, never stored with the entry: the first
    request's Host header or query parameters would reach everybody else."""
    for value in (*args, *kwargs.values()):
        if isinstance(value, PageParams | LimitOffsetParams | CursorParams) and (
            value.links is not None
        ):
            value.links.add(result)


def _targets(response: Response | None, result: object) -> list[Response]:
    """The injected response, plus the result when the endpoint returned a Response itself:
    FastAPI ignores the injected response's headers then."""
    targets = [] if response is None else [response]
    if isinstance(result, Response) and result is not response:
        targets.append(result)
    return targets


def _warn_unsigned_once(coder: type[Coder]) -> None:
    if coder not in _warned_coders:
        _warned_coders.add(coder)
        warn_unsigned(coder)


def _pick(value: Any, default: Any) -> Any:
    return default if value is None else value


@dataclass
class _Endpoint:
    """What @cache() knows about the decorated function, built once."""

    func: Callable[..., Any]
    signature: Signature
    return_type: Any
    namespace: str
    expire: int | None
    coder: type[Coder] | None
    key_builder: KeyBuilder | None
    exclude_params: Collection[str] | None
    vary_headers: tuple[str, ...] | None
    respect_cache_control: bool | None
    stale_while_revalidate: int | None
    single_flight: bool | None
    max_value_size: int | None
    distributed_lock: bool | None
    early_refresh: float | None
    tags: Tags | None
    #: The Cache this function was decorated with (Cache.cached); None: FastAPICache's.
    instance: Cache | None = None
    key_checked: bool = field(default=False)


class _CachedCall:
    """One call of a cached function: lookup, then compute and store on a miss."""

    # Set once the call goes through the cache (not a bypass): _execute(), then _build_key().
    coder: type[Coder]
    expire: int | None
    backend: Backend
    guard: BackendGuard
    protector: EntryProtector | None
    cache_key: str
    policy: _HeaderPolicy
    if_none_match: str | None

    def __init__(
        self,
        endpoint: _Endpoint,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        request: Request | None,
        response: Response | None,
        run: Callable[[], Awaitable[Any]],
    ) -> None:
        self.endpoint = endpoint
        self.args = args
        self.kwargs = kwargs
        self.request = request
        self.response = response
        self.run = run
        self.config: Cache = (
            endpoint.instance if endpoint.instance is not None else FastAPICache.instance()
        )
        self.options: CacheOptions = self.config.options
        self.metrics = self.config.metrics
        self.payload: bytes | None = None  # what this call stored, for single flight
        self.span: Any = None
        # For Cache-Status: why the request reached the endpoint, and whether its result was kept.
        self.forward = "miss"
        self.stored = False

    async def execute(self) -> Any:
        function = getattr(self.endpoint.func, "__qualname__", repr(self.endpoint.func))
        with self.config.tracer.call(function, self.endpoint.namespace) as span:
            self.span = span
            return await self._execute()

    def _lookup(self, result: str) -> None:
        self.metrics.lookup(result, self.endpoint.namespace)
        if self.span is not None:
            self.span.set_attribute("fastapi_alcazaba.result", result)

    async def _execute(self) -> Any:
        endpoint, options = self.endpoint, self.options
        if not self.config.enable:
            self._lookup("bypass")
            return self._report(await self.run(), "fwd=bypass")
        if self.request is not None and self.request.method != "GET":
            self._lookup("bypass")
            return self._report(await self.run(), "fwd=method")
        respect = _pick(endpoint.respect_cache_control, options.respect_cache_control)
        directives = _cache_control_directives(self.request) if respect else frozenset()
        if "no-store" in directives:
            self._lookup("bypass")
            return self._report(await self.run(), "fwd=request")

        self.coder = endpoint.coder or self.config.coder
        self.expire = endpoint.expire or self.config.expire
        self.backend = self.config.backend
        self.guard = self.config.guard
        self.protector = options.protector
        if self.coder.requires_signing and self.protector is None:
            _warn_unsigned_once(self.coder)
        await self._build_key()

        stale: bytes | None = None
        if "no-cache" in directives:
            self.forward = "request"  # the client's Cache-Control didn't let us use an entry
        elif (entry := await self._read()) is not None:
            ttl, payload = entry
            if self._refresh_early(ttl):
                stale = payload
                self.forward = "stale"
            elif (served := self._serve(payload, _hit(ttl), ttl if ttl > 0 else None)) is not (
                _UNUSABLE
            ):
                self._lookup("hit")
                return served
        self._lookup("refresh" if stale is not None else "miss")
        if stale is None:
            return await self._miss()
        try:
            return await self._miss()
        except Exception:
            # Refreshing early is optional: until it expires, the entry we have is still valid.
            logger.warning(
                "Early refresh of %r failed; serving the cached value",
                self.cache_key,
                exc_info=True,
            )
            served = self._serve(stale, "hit", None)
            if served is _UNUSABLE:
                raise
            return served

    # region Key

    async def _build_key(self) -> None:
        endpoint = self.endpoint
        key_builder = endpoint.key_builder or self.config.key_builder
        key_args, key_kwargs = (
            _exclude_params(endpoint.signature, endpoint.exclude_params, self.args, self.kwargs)
            if endpoint.exclude_params
            else (self.args, self.kwargs)
        )
        if not endpoint.key_checked:
            endpoint.key_checked = True
            if key_builder is default_key_builder:
                _warn_unstable_key(endpoint.func, endpoint.signature, key_args, key_kwargs)
        built_key = key_builder(
            endpoint.func,
            f"{self.config.prefix}:{endpoint.namespace}",
            request=self.request,
            response=self.response,
            args=key_args,
            kwargs=key_kwargs,
        )
        cache_key = await built_key if isawaitable(built_key) else built_key
        if not isinstance(cache_key, str):
            raise TypeError(f"the key builder returned {type(cache_key).__name__}, not str")
        # Whatever the key builder, a user's response is never served to another one.
        vary = _pick(endpoint.vary_headers, self.options.vary_headers)
        vary_digest = _vary_digest(self.request, vary)
        if vary_digest is not None:
            cache_key = f"{cache_key}:vary:{vary_digest}"
        self.cache_key = cache_key
        self.policy = _HeaderPolicy(
            status_name=self.config.cache_status,
            vary=vary,
            private=vary_digest is not None,
            stale_while_revalidate=endpoint.stale_while_revalidate,
        )
        self.if_none_match = (
            self.request.headers.get("if-none-match") if self.request is not None else None
        )

    # endregion

    # region Backend

    def _backend_failed(self, operation: str, exc: BackendUnavailable) -> None:
        if isinstance(exc, CircuitOpen):
            logger.debug("Skipping %s of %r: %s", operation, self.cache_key, exc)
        else:
            logger.warning("Backend %s of %r failed", operation, self.cache_key, exc_info=exc)

    async def _read(self) -> tuple[int, bytes] | None:
        try:
            ttl, stored = await self.guard.run(
                "get", lambda: self.backend.get_with_ttl(self.cache_key)
            )
        except BackendUnavailable as exc:
            self._backend_failed("get", exc)
            return None
        if stored is None:
            return None
        opened = stored if self.protector is None else self.protector.open(self.cache_key, stored)
        if opened is None:
            logger.warning(
                "Ignoring cache entry %r: it isn't signed or encrypted by us for this key",
                self.cache_key,
            )
            return None
        payload = decompress(opened, self.options.decompression_limit)
        if payload is None:
            logger.warning("Ignoring cache entry %r: it can't be decompressed", self.cache_key)
            return None
        return ttl, payload

    def _refresh_early(self, ttl: int) -> bool:
        """Probabilistic early refresh: in the last `early_refresh` part of the lifetime, recompute
        with a probability that grows to 1 at expiry, so that one caller refreshes a popular entry
        before everybody misses it at once."""
        fraction = _pick(self.endpoint.early_refresh, self.options.early_refresh)
        if not fraction or not self.expire or ttl <= 0:
            return False
        window = float(self.expire * fraction)
        return ttl < window and random.random() >= ttl / window  # noqa: S311  # not for security

    def _tags(self) -> list[str]:
        tags = self.endpoint.tags
        if tags is None:
            return []
        try:
            names = list(tags(*self.args, **self.kwargs) if callable(tags) else tags)
        except Exception:
            logger.warning(
                "Not tagging %r: computing its tags failed", self.cache_key, exc_info=True
            )
            return []
        return [self.config.tag_key(name) for name in names]

    async def _store(self, payload: bytes) -> bool:
        compressor = self.options.compressor
        packed = payload if compressor is None else compressor.compress(payload)
        stored = packed if self.protector is None else self.protector.seal(self.cache_key, packed)
        try:
            await self.guard.run(
                "set", lambda: self.backend.set(self.cache_key, stored, self.expire)
            )
        except BackendUnavailable as exc:
            self._backend_failed("set", exc)
            return False
        tags = self._tags()
        if tags and self.backend.supports_tags:
            try:
                await self.guard.run(
                    "tag", lambda: self.backend.add_tags(self.cache_key, tags, self.expire)
                )
            except BackendUnavailable as exc:
                self._backend_failed("tag", exc)
        elif tags:
            logger.warning(
                "%s can't store tags: %r is untagged", type(self.backend).__name__, self.cache_key
            )
        return True

    # endregion

    # region Miss

    def _encode(self, result: Any) -> tuple[str, bytes | None]:
        if not _is_cacheable(result, self.response):
            return "uncacheable", None
        try:
            payload = self.coder.encode(result)
        except Exception:
            logger.warning(
                "Not caching %r: the result can't be encoded", self.cache_key, exc_info=True
            )
            return "unencodable", None
        limit = _pick(self.endpoint.max_value_size, self.options.max_value_size)
        if limit is not None and len(payload) > limit:
            logger.warning(
                "Not caching %r: %d bytes, more than max_value_size (%d)",
                self.cache_key,
                len(payload),
                limit,
            )
            return "too_large", None
        return "stored", payload

    async def _compute(self) -> Any:
        result = await self.run()
        outcome, payload = self._encode(result)
        if payload is not None and not await self._store(payload):
            outcome = "failed"
        self.metrics.store(
            outcome,
            self.endpoint.namespace,
            len(payload) if outcome == "stored" and payload else None,
        )
        self.payload = payload
        self.stored = outcome == "stored"
        return self._finish(result, payload)

    async def _miss(self) -> Any:
        if not _pick(self.endpoint.single_flight, self.options.single_flight):
            return await self._produce()
        loop = asyncio.get_running_loop()
        flight = (id(loop), self.cache_key)
        leader = _in_flight.get(flight)
        if leader is not None:
            payload = await asyncio.shield(leader)
            if (
                payload is not None
                and (served := self._serve(payload, f"fwd={self.forward}; collapsed", self.expire))
                is not _UNUSABLE
            ):
                return served
            return await self._produce()
        future: asyncio.Future[bytes | None] = loop.create_future()
        _in_flight[flight] = future
        try:
            return await self._produce()
        finally:
            # Waiting calls get the payload; on errors or uncacheable results they compute.
            del _in_flight[flight]
            future.set_result(self.payload)

    async def _produce(self) -> Any:
        """Compute the value; with distributed_lock, only the process holding the lock does."""
        if not (
            _pick(self.endpoint.distributed_lock, self.options.distributed_lock)
            and self.backend.supports_locks
        ):
            return await self._compute()
        lock_key, token = f"{self.cache_key}:lock", secrets.token_hex(16)
        try:
            acquired = await self.guard.run(
                "lock",
                lambda: self.backend.acquire_lock(lock_key, token, self.options.lock_timeout),
            )
        except BackendUnavailable as exc:
            self._backend_failed("lock", exc)
            return await self._compute()  # without the backend there's nothing to wait for
        if not acquired:
            if (served := await self._wait_for_entry()) is not _UNUSABLE:
                return served
            return await self._compute()
        try:
            # Another process may have stored the value and released the lock between our miss
            # and our acquire: check again before computing it (double-checked locking).
            entry = await self._read()
            if entry is not None and (served := self._serve_entry(entry)) is not _UNUSABLE:
                return served
            return await self._compute()
        finally:
            try:
                await self.guard.run("unlock", lambda: self.backend.release_lock(lock_key, token))
            except BackendUnavailable as exc:
                self._backend_failed("unlock", exc)  # it expires after lock_timeout anyway

    async def _wait_for_entry(self) -> Any:
        """Poll for the entry another process is computing, until lock_wait runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.lock_wait
        delay = 0.025
        while (remaining := deadline - loop.time()) > 0:
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.4)
            if (entry := await self._read()) is not None:
                served = self._serve_entry(entry)
                if served is not _UNUSABLE:
                    return served
        return _UNUSABLE

    def _serve_entry(self, entry: tuple[int, bytes]) -> Any:
        """Serve an entry another process stored. The calls waiting for this one in the same
        process (single flight) reuse it instead of computing it again."""
        ttl, payload = entry
        served = self._serve(payload, _hit(ttl), ttl if ttl > 0 else None)
        if served is not _UNUSABLE:
            self.payload = payload
        return served

    # endregion

    # region Response

    def _not_modified(self, etag: str, status: str, max_age: int | None) -> Response | None:
        if self.response is None or not _etag_matches(self.if_none_match, etag):
            return None
        self.policy.apply([self.response], status, etag, max_age)
        self.response.status_code = HTTP_304_NOT_MODIFIED
        return self.response

    def _serve(self, payload: bytes, status: str, max_age: int | None) -> Any:
        """The result from cached bytes, or _UNUSABLE when they don't decode."""
        etag = _compute_etag(payload)
        if (unchanged := self._not_modified(etag, status, max_age)) is not None:
            return unchanged
        try:
            result = self.coder.decode_as_type(payload, type_=self.endpoint.return_type)
        except Exception:
            # Corrupt, or written before the return type changed: recompute it.
            logger.warning(
                "Ignoring cache entry %r: it can't be decoded", self.cache_key, exc_info=True
            )
            return _UNUSABLE
        self.policy.apply(_targets(self.response, result), status, etag, max_age)
        _add_page_links(result, self.args, self.kwargs)
        return result

    def _finish(self, result: Any, payload: bytes | None) -> Any:
        status = f"fwd={self.forward}; stored" if self.stored else f"fwd={self.forward}"
        if payload is None:  # not cacheable: no ETag nor Cache-Control for it
            return self._report(result, status)
        etag = _compute_etag(payload)
        if (unchanged := self._not_modified(etag, status, self.expire)) is not None:
            return unchanged
        self.policy.apply(_targets(self.response, result), status, etag, self.expire)
        return result

    def _report(self, result: Any, status: str) -> Any:
        """The result, with this cache's Cache-Status on the response."""
        for target in _targets(self.response, result):
            _add_cache_status(target, self.config.cache_status, status)
        return result

    # endregion


class _CacheDecorator(Protocol):
    @overload
    def __call__(
        self,
        func: Callable[P, Awaitable[R]],
        /,
    ) -> Callable[P, Awaitable[R | Response]]: ...

    @overload
    def __call__(self, func: Callable[P, R], /) -> Callable[P, Awaitable[R | Response]]: ...


def cache(
    expire: int | None = None,
    coder: type[Coder] | None = None,
    key_builder: KeyBuilder | None = None,
    namespace: str = "",
    injected_dependency_namespace: str = "__fastapi_alcazaba",
    *,
    exclude_params: Collection[str] | None = None,
    vary_headers: Iterable[str] | None = None,
    respect_cache_control: bool | None = None,
    stale_while_revalidate: int | None = None,
    single_flight: bool | None = None,
    max_value_size: int | None = None,
    distributed_lock: bool | None = None,
    early_refresh: float | None = None,
    tags: Tags | None = None,
    instance: Cache | None = None,
) -> _CacheDecorator:
    """Cache the result of an endpoint, or of any function, sync or async.

    Settings left as None use the ones given to FastAPICache.init().

    :param expire: seconds to keep the entry.
    :param coder: how the result is stored (JsonCoder by default).
    :param key_builder: builds the cache key from the function and its arguments.
    :param namespace: groups entries, to delete them together with FastAPICache.clear().
    :param injected_dependency_namespace: prefix of the request and response parameters added
        to the endpoint when it doesn't declare them.
    :param exclude_params: arguments left out of the cache key: calls that only differ in them
        share an entry (e.g. a nonce, or a database session whose repr changes with each call).
    :param vary_headers: request headers that give each value its own entry and make the
        response `private`; `()` when the response is the same for everybody.
    :param respect_cache_control: let clients skip the cache with Cache-Control no-cache or
        no-store.
    :param stale_while_revalidate: adds that directive to Cache-Control.
    :param single_flight: concurrent misses for the same key wait for the first one.
    :param max_value_size: don't store values larger than this many bytes.
    :param distributed_lock: one process computes a missing value while the others wait.
    :param early_refresh: refresh popular entries before they expire (a fraction of `expire`).
    :param instance: the Cache to use instead of the global FastAPICache (what Cache.cached()
        passes).
    :param tags: names to invalidate the entry by, with FastAPICache.invalidate_tags(); a list,
        or a function receiving the call's arguments, e.g. `lambda product_id, **_:
        [f"product:{product_id}"]`.
    """
    if early_refresh is not None and not 0 < early_refresh <= 1:
        raise ValueError("early_refresh is a fraction of the lifetime, between 0 and 1")
    if max_value_size is not None and max_value_size < 1:
        raise ValueError("max_value_size must be at least 1 byte")

    injected_request = Parameter(
        name=f"{injected_dependency_namespace}_request",
        annotation=Request,
        kind=Parameter.KEYWORD_ONLY,
    )
    injected_response = Parameter(
        name=f"{injected_dependency_namespace}_response",
        annotation=Response,
        kind=Parameter.KEYWORD_ONLY,
    )
    endpoint_vary = None if vary_headers is None else normalise_headers(vary_headers)
    static_tags = None if tags is None or callable(tags) else tuple(tags)

    def wrapper(
        func: Callable[P, Awaitable[R]] | Callable[P, R],
    ) -> Callable[P, Awaitable[R | Response]]:
        # get_typed_signature ensures that any forward references are resolved first
        wrapped_signature = get_typed_signature(func)
        to_inject: list[Parameter] = []
        request_param = _locate_param(wrapped_signature, injected_request, to_inject)
        response_param = _locate_param(wrapped_signature, injected_response, to_inject)
        return_type = get_typed_return_annotation(func)
        if exclude_params:
            _check_excluded_params(wrapped_signature, exclude_params)
        endpoint = _Endpoint(
            func=func,
            signature=wrapped_signature,
            return_type=return_type,
            namespace=namespace,
            expire=expire,
            coder=coder,
            key_builder=key_builder,
            exclude_params=exclude_params,
            vary_headers=endpoint_vary,
            respect_cache_control=respect_cache_control,
            stale_while_revalidate=stale_while_revalidate,
            single_flight=single_flight,
            max_value_size=max_value_size,
            distributed_lock=distributed_lock,
            early_refresh=early_refresh,
            tags=static_tags if static_tags is not None else tags,
            instance=instance,
        )

        @wraps(func)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> R | Response:
            async def ensure_async_func() -> R:
                """Run cached sync functions in thread pool just like FastAPI."""
                # if the wrapped function does NOT have request or response in
                # its function signature, make sure we don't pass them in as
                # keyword arguments
                call_kwargs = dict(kwargs)
                call_kwargs.pop(injected_request.name, None)
                call_kwargs.pop(injected_response.name, None)
                if iscoroutinefunction(func):
                    return await func(*args, **call_kwargs)  # type: ignore[arg-type,no-any-return]
                # sync: wrap in a thread, as FastAPI does
                return await run_in_threadpool(func, *args, **call_kwargs)  # type: ignore[arg-type]

            copy_kwargs = kwargs.copy()
            request = cast(Request | None, copy_kwargs.pop(request_param.name, None))
            response = cast(Response | None, copy_kwargs.pop(response_param.name, None))
            call = _CachedCall(endpoint, args, copy_kwargs, request, response, ensure_async_func)
            return cast(R | Response, await call.execute())

        signature = _augment_signature(wrapped_signature, *to_inject)
        if return_type is not None and (
            (isinstance(return_type, type) and issubclass(return_type, Response))
            or type_adapter_for(return_type) is not None
        ):
            # FastAPI takes the response model from the return annotation.
            signature = signature.replace(return_annotation=return_type)
        inner.__signature__ = signature  # type: ignore[attr-defined]

        return inner

    return cast(_CacheDecorator, wrapper)
