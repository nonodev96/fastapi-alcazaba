# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First release of Alcazaba, forked from fastapi-cache2 0.2.2. The "Migrating from fastapi-cache2"
section of the README lists the behaviour changes; UPSTREAM.md, what happened to every upstream
pull request.

### Security

- Per-user cache entries for requests with `Authorization` or `Cookie` (`vary_headers`), sent as
  `Cache-Control: private` with `Vary`. fastapi-cache2 could serve one user's response to another.
- `RedisBackend.clear(namespace=...)` no longer builds a Lua script from the namespace (command
  injection) nor runs `KEYS` (blocks the server): `SCAN` + `UNLINK`, with glob characters escaped.
- Optional HMAC-SHA256 signing of entries, bound to their key (`signing_key`, with rotation).
  `PickleCoder` without it raises an `UnsignedEntriesWarning`.
- `InMemoryBackend` is bounded (`max_size`, LRU) and no longer shared between instances.
- Clients can no longer bypass the cache with `Cache-Control` unless `respect_cache_control=True`.
- Only 200 responses without `no-store` are cached.
- JSON type markers can no longer be triggered by user data.
- SHA-256 instead of MD5 for keys and ETags.
- Cache stampede protection (`single_flight`).
- The Redis example no longer serves its working directory as static files.
- `UnstableCacheKeyWarning` when the default key includes an argument whose `repr()` changes on
  every call (a database session, an injected service), which made every request a miss.
- Backend calls have a timeout (`backend_timeout`) and go through a circuit breaker
  (`circuit_breaker_failures`, `circuit_breaker_reset`): a hanging backend no longer slows every
  request down.
- `max_value_size` keeps oversized values out of the backend.
- Optional encryption of the entries (`encryption_key`, `crypto` extra): AES-256-GCM with a
  per-entry key derived by HKDF and the cache key as associated data, with key rotation.

### Added

- `ValkeyBackend` (valkey-py) and `GlideBackend` (Valkey GLIDE), standalone and cluster.
- Invalidation by tag: `@cache(tags=...)` and `FastAPICache.invalidate_tags()` (in-memory,
  Redis, Valkey, GLIDE).
- `distributed_lock`: one process computes a missing value while the others wait for it.
- `early_refresh`: probabilistic refresh of entries near their expiry; a failed refresh serves
  the cached value.
- OpenTelemetry metrics (`otel` extra): lookups, stores, entry sizes, backend durations and
  errors; and traces: a span per cached call with its result, and one per backend call.
- `Cache` instances (one per app, or several with different settings) with their own `cached()`
  decorator, `get_or_set()` for caching inside endpoints, `invalidate_tags()`, `clear()` and
  `close()`, and the `get_cache` FastAPI dependency. `FastAPICache` is now a global `Cache`.
- `TieredBackend`: a per-process memory level in front of any backend, kept in step across
  processes by pub/sub invalidations (Redis, Valkey) or bounded by `local_ttl`.
- `GlideBackend` reads TTL and value in one round trip (a batch) instead of two.
- Compression of large entries (`compression="zstd"` or `"zlib"`), read back whatever the setting
  and bounded against decompression bombs.
- `JsonCoder` encodes through pydantic-core and validates hits straight from the bytes: 8 times
  faster encoding of models, 1.4 times faster hits.
- `exclude_params`; `stale_while_revalidate`; `MemcachedBackend` and `DynamoBackend` can delete
  single keys; `DynamoBackend(endpoint_url=...)`; `cache_status_header=""` turns the header off.

### Fixed

- Cache hits return the annotated return type again (Pydantic models, dataclasses).
- Deterministic, quoted ETags; `If-None-Match` lists, weak tags and `*`.
- The response model appears in OpenAPI again.
- No `Cache-Control: max-age=None`; headers also reach `Response`s returned by the endpoint.
- `FastAPICache.clear(key=...)` with a prefix no longer clears the whole prefix.
- Entries that can't be decoded are recomputed instead of failing.
- Importing works without jinja2 (Starlette 1.x).
- In-memory entries without `expire` no longer expire immediately; memcached expirations over 30
  days; the settings of one call no longer leak into the next.

### Changed

- Distribution `fastapi-alcazaba`, import package `fastapi_alcazaba`. Python 3.11+ (3.10 reached its end
  of life in October 2026), Pydantic v2.
- `redis` is optional again; `uvicorn` and `pendulum` are no longer dependencies.

## fastapi-cache2 0.2

### 0.2.1
- Fix picklecoder
- Fix connection failure transparency and add logging
- Add Cache-Control and ETag on first response
- Support Async RedisCluster client from redis-py

### 0.2.0

- Make `request` and `response` optional.
- Add typing info to the `cache` decorator.
- Support cache jinja2 template response.
- Support cache `JSONResponse`
- Add `py.typed` file and type hints
- Add TestCase
- Fix cache decorate sync function
- Transparently handle backend connection failures.

## fastapi-cache2 0.1

### 0.1.10

- Add `Cache-Control:no-cache` support.

### 0.1.9

- Replace `aioredis` with `redis-py`.

### 0.1.8

- Support `dynamodb` backend.

### 0.1.7

- Fix default json coder for datetime.
- Add `enable` param to `init`.

### 0.1.6

- Fix redis cache.
- Encode key builder.

### 0.1.5

- Fix setting expire for redis (#24)
- Update expire key

### 0.1.4

- Fix default expire for memcached. (#13)
- Update default key builder. (#12)

### 0.1.3

- Fix cache key builder.

### 0.1.2

- Add default config when init.
- Update JsonEncoder.

### 0.1.1

- Add in-memory support.

### 0.1.0

- First version release.
