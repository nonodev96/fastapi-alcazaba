"""Alcazaba: the pieces most FastAPI apps end up writing themselves, a folder per tool.

The main class of each tool is importable from here; the rest of a tool, from its folder
(`fastapi_alcazaba.rate_limit`...). The pagination strategies are each in a module of their own
(`fastapi_alcazaba.pagination.cursor`...).
"""

from importlib.metadata import PackageNotFoundError, version

from fastapi_alcazaba.audit import FastAPIAudit
from fastapi_alcazaba.auth import FastAPIAPIKeys, FastAPIAuth
from fastapi_alcazaba.authz import FastAPICasbin
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
    "Backend",
    "BackendUnavailable",
    "BreakerOpen",
    "Cache",
    "CacheOptions",
    "Coder",
    "DEFAULT_VARY_HEADERS",
    "FastAPIAPIKeys",
    "FastAPIAudit",
    "FastAPIAuth",
    "FastAPIBreaker",
    "FastAPICache",
    "FastAPICasbin",
    "FastAPIConcurrencyLimiter",
    "FastAPIFeatureFlags",
    "FastAPILeader",
    "FastAPIMaintenance",
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
