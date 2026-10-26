from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, ClassVar

from fastapi_alcazaba.apikeys import FastAPIAPIKeys
from fastapi_alcazaba.breaker import BreakerOpen, FastAPIBreaker
from fastapi_alcazaba.cache import (
    DEFAULT_VARY_HEADERS,
    Cache,
    CacheOptions,
    get_cache,
    normalise_headers,
    warn_unsigned,
)
from fastapi_alcazaba.coder import Coder, JsonCoder
from fastapi_alcazaba.concurrency import FastAPIConcurrencyLimiter
from fastapi_alcazaba.errors import (
    BackendUnavailable,
    UnsignedEntriesWarning,
    UnstableCacheKeyWarning,
)
from fastapi_alcazaba.flags import FastAPIFeatureFlags
from fastapi_alcazaba.key_builder import default_key_builder
from fastapi_alcazaba.leader import FastAPILeader
from fastapi_alcazaba.limiter import FastAPIRateLimiter
from fastapi_alcazaba.maintenance import FastAPIMaintenance
from fastapi_alcazaba.metrics import Metrics
from fastapi_alcazaba.pagination import (
    FastAPIPagination,
    FastAPIPaginationCursor,
    FastAPIPaginationLimitOffset,
)
from fastapi_alcazaba.problems import FastAPIProblemDetails, Problem
from fastapi_alcazaba.resilience import BackendGuard
from fastapi_alcazaba.scheduler import FastAPIScheduler
from fastapi_alcazaba.tasks import FastAPITaskQueue
from fastapi_alcazaba.types import Backend, KeyBuilder
from fastapi_alcazaba.uploads import FastAPIUploads
from fastapi_alcazaba.webhooks import FastAPIWebhooks

if TYPE_CHECKING:
    from fastapi_alcazaba.locks import Lock

try:
    __version__ = version("fastapi-alcazaba")
except PackageNotFoundError:  # running from a source checkout that isn't installed
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_VARY_HEADERS",
    "Backend",
    "BackendUnavailable",
    "BreakerOpen",
    "Cache",
    "CacheOptions",
    "Coder",
    "FastAPIAPIKeys",
    "FastAPIBreaker",
    "FastAPICache",
    "FastAPIConcurrencyLimiter",
    "FastAPIFeatureFlags",
    "FastAPILeader",
    "FastAPIMaintenance",
    "FastAPIPagination",
    "FastAPIPaginationCursor",
    "FastAPIPaginationLimitOffset",
    "FastAPIProblemDetails",
    "FastAPIRateLimiter",
    "FastAPIScheduler",
    "FastAPITaskQueue",
    "FastAPIUploads",
    "FastAPIWebhooks",
    "JsonCoder",
    "KeyBuilder",
    "Problem",
    "UnsignedEntriesWarning",
    "UnstableCacheKeyWarning",
    "default_key_builder",
    "get_cache",
    "normalise_headers",
    "warn_unsigned",
]


class FastAPICache:
    """The global cache that `@cache` uses: a Cache configured once with `init()`. The API of
    fastapi-cache2; for several caches, or one per app, create Cache instances instead."""

    _instance: ClassVar[Cache | None] = None

    @classmethod
    def init(cls, backend: Backend, *args: Any, **kwargs: Any) -> None:
        """Configure the global cache, with the arguments of `Cache`; later calls are ignored
        until `reset()`."""
        if cls._instance is None:
            cls._instance = Cache(backend, *args, **kwargs)

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    @classmethod
    def instance(cls) -> Cache:
        if cls._instance is None:
            raise RuntimeError(
                "FastAPICache.init() hasn't been called (or decorate with a Cache's cached())"
            )
        return cls._instance

    @classmethod
    def get_backend(cls) -> Backend:
        return cls.instance().backend

    @classmethod
    def get_prefix(cls) -> str:
        return cls.instance().prefix

    @classmethod
    def get_expire(cls) -> int | None:
        return cls._instance.expire if cls._instance is not None else None

    @classmethod
    def get_coder(cls) -> type[Coder]:
        return cls.instance().coder

    @classmethod
    def get_key_builder(cls) -> KeyBuilder:
        return cls.instance().key_builder

    @classmethod
    def get_cache_status_header(cls) -> str:
        return cls.instance().cache_status_header

    @classmethod
    def get_enable(cls) -> bool:
        return cls._instance.enable if cls._instance is not None else True

    @classmethod
    def get_options(cls) -> CacheOptions:
        return cls._instance.options if cls._instance is not None else CacheOptions()

    @classmethod
    def get_guard(cls) -> BackendGuard:
        return cls.instance().guard

    @classmethod
    def get_metrics(cls) -> Metrics:
        return cls._instance.metrics if cls._instance is not None else Metrics(enabled=False)

    @classmethod
    def tag_key(cls, tag: str) -> str:
        return cls.instance().tag_key(tag)

    @classmethod
    async def invalidate_tags(cls, *tags: str) -> int:
        """Delete every entry stored with any of `tags` (see `@cache(tags=...)`)."""
        return await cls.instance().invalidate_tags(*tags)

    @classmethod
    def lock(
        cls, name: str, *, ttl: float = 30.0, wait: float | None = 0.0, renew: bool = True
    ) -> "Lock":
        """A lock shared by every process (see `Cache.lock`)."""
        return cls.instance().lock(name, ttl=ttl, wait=wait, renew=renew)

    @classmethod
    async def clear(cls, namespace: str | None = None, key: str | None = None) -> int:
        """Delete the entry `key` (a full cache key), or every entry of `namespace`, or of the
        whole prefix when neither is given."""
        return await cls.instance().clear(namespace, key)
