"""Alcazaba: the pieces most FastAPI apps end up writing themselves, a folder per tool.

The main class of each tool is importable from here; the rest of a tool, from its folder
(`fastapi_alcazaba.pagination`, `fastapi_alcazaba.rate_limit`...).
"""

from importlib.metadata import PackageNotFoundError, version

from fastapi_alcazaba.audit import FastAPIAudit
from fastapi_alcazaba.auth import FastAPIAPIKeys
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.background import FastAPILeader, FastAPIScheduler, FastAPITaskQueue
from fastapi_alcazaba.core import (
    BackendUnavailable,
    UnsignedEntriesWarning,
    UnstableCacheKeyWarning,
)
from fastapi_alcazaba.observability import FastAPIProblemDetails, Problem
from fastapi_alcazaba.operations import (
    BreakerOpen,
    FastAPIBreaker,
    FastAPIFeatureFlags,
    FastAPIMaintenance,
)
from fastapi_alcazaba.pagination import (
    FastAPIPagination,
    FastAPIPaginationCursor,
    FastAPIPaginationLimitOffset,
)
from fastapi_alcazaba.rate_limit import FastAPIConcurrencyLimiter, FastAPIRateLimiter
from fastapi_alcazaba.response_cache import (
    DEFAULT_VARY_HEADERS,
    Cache,
    CacheOptions,
    Coder,
    FastAPICache,
    JsonCoder,
    KeyBuilder,
    default_key_builder,
    get_cache,
)
from fastapi_alcazaba.response_cache.caches import normalise_headers, warn_unsigned
from fastapi_alcazaba.uploads import FastAPIUploads
from fastapi_alcazaba.webhooks import FastAPIWebhooks

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
    "FastAPIAudit",
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
