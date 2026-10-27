"""The response cache: `@cache` for endpoints and functions, `Cache` and the global
`FastAPICache`, key builders and coders."""

from fastapi_alcazaba.response_cache.caches import (
    DEFAULT_VARY_HEADERS,
    Cache,
    CacheOptions,
    FastAPICache,
    get_cache,
)
from fastapi_alcazaba.response_cache.coder import Coder, JsonCoder, PickleCoder
from fastapi_alcazaba.response_cache.decorator import cache
from fastapi_alcazaba.response_cache.key_builder import KeyBuilder, default_key_builder

__all__ = [
    "Cache",
    "cache",
    "CacheOptions",
    "Coder",
    "default_key_builder",
    "DEFAULT_VARY_HEADERS",
    "FastAPICache",
    "get_cache",
    "JsonCoder",
    "KeyBuilder",
    "PickleCoder",
]
