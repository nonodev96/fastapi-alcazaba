# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Rate limiting: `FastAPIRateLimiter` (`fastapi_alcazaba.limiter`), a FastAPI dependency that lets
  each client make `times` requests per sliding window to a route, with a 429 and `Retry-After`
  beyond. The counters live in the cache's backend, shared by every process, behind its timeout
  and circuit breaker (`fail_open`). A port of fastapi-limiter without its weaknesses: clients are
  told apart by the address the ASGI server reports, not by `X-Forwarded-For` (which any client
  can set), and IPv6 addresses by their /64 network (`client_network` for other sizes); the
  budget is per route, not per URL (`/items/1`, `/items/2`… had one each); requests over the
  limit don't count, checked and counted atomically. `name` shares a budget across routes, `cost`
  makes a route take more of it, `identifier` counts per user, and `hit()` limits anything else
  (WebSocket messages, login attempts). Responses carry the `RateLimit` and `RateLimit-Policy`
  headers of the IETF draft. Each process refuses a client with nothing left without asking the
  backend until something fits again.
- Counters in every backend, for the rate limiter: `Backend.incr`, `Backend.get_counter`,
  `Backend.incr_window` (a Lua script on Redis, Valkey and GLIDE, a conditional write on
  DynamoDB) and `Backend.supports_counters`.
- The `fastapi_alcazaba.rate_limit` metric.
- `get_cache` works in WebSocket routes.
- Pagination without another library (`fastapi_alcazaba.pagination`): `FastAPIPagination` (page
  and size), `FastAPIPaginationLimitOffset` and `FastAPIPaginationCursor`, each configured once
  with its limits and used as a dependency that checks the query parameters, and with a
  `paginate` for a whole list or a page already queried. `Page` and `LimitOffsetPage` have the
  JSON of fastapi-pagination's. Cursors are keyset pagination on a unique sort key that keeps its
  types (datetimes, UUIDs, decimals…), optionally signed (HMAC-SHA256, rotation), with a 400 for
  invalid ones. The page parameters are arguments of the endpoint, so `@cache` keeps each page
  under its own key. An example (`examples/pagination`): a catalogue cached page by page, whole,
  or by cursor.
- Locks shared by every process: `Cache.lock()` / `FastAPICache.lock()` (`fastapi_alcazaba.locks`),
  with an expiry, renewal while held, a random token per holder, waiting (`wait`) and
  `LockNotAcquired`. `InMemoryBackend` (within the process) and `DynamoBackend` (conditional
  writes) can now hold locks, and backends gain `extend_lock`.
- Idempotency keys: `IdempotencyMiddleware` and `require_idempotency_key`
  (`fastapi_alcazaba.idempotency`, draft-ietf-httpapi-idempotency-key-header). A retried POST or
  PATCH gets the first response (`Idempotent-Replayed: true`) and runs once; a retry while it
  runs gets a 409, the same key for another request a 422. Keys are per client (`Authorization`
  by default), responses are stored signed or encrypted, and a failing backend answers 503 unless
  `fail_open=True`.
- Revoked tokens and one-time codes (`fastapi_alcazaba.tokens`): `TokenBlocklist` keeps revoked
  JWTs (`revoke`, by `jti`) and subjects logged out everywhere (`revoke_subject`) in the cache's
  backend until the tokens would have expired, and refuses them when the backend fails unless
  `fail_open=True`. `OneTimeCodes` issues e-mail or SMS codes, stored as HMACs with a secret,
  with an expiry, a limit of guesses and single use that holds under concurrent requests.
- Outbound calls: `ResilientTransport` (`fastapi_alcazaba.outbound`), an httpx2/httpx transport
  that gives each call a deadline for the whole response (httpx's timeouts are per phase, so a
  server that trickles bytes kept a request open forever) and a circuit breaker per origin
  (`CircuitOpenError`, an `httpx.TransportError`). 5xx responses count as failures
  (`failure_statuses`), the caller's own errors (an illegal header, an unsupported scheme) don't.
  `max_response_size` caps the bytes read (`ResponseTooLargeError`).
- `SecurityHeadersMiddleware` and `BodySizeLimitMiddleware` (`fastapi_alcazaba.middleware`): the
  OWASP REST security headers on every response (HSTS only over HTTPS, a strict CSP for
  everything but HTML), and a 413 for request bodies over a size limit, checked from
  `Content-Length` and counted for chunked bodies.
- Health checks: `health_router()` (`fastapi_alcazaba.health`) adds `/health/live` (the process
  answers) and `/health/ready` (the app's checks run concurrently with timeouts: 200 `ok` or
  `degraded`, 503 `fail`), with a non-critical check of the cache's backend. Errors are logged,
  never returned, and concurrent probes share one run, reused for `reuse_for` seconds.
- Scheduled jobs: `FastAPIScheduler` (`fastapi_alcazaba.scheduler`) runs async or sync jobs
  every N seconds (`every`) or by a cron expression in a time zone (`cron`, with its own parser:
  ranges, steps, names, `@daily`…; clock changes handled as cronie does), in every process and
  each tick once across them: ticks are claimed with an atomic counter in the backend, and a lock
  held while a job runs keeps long runs from overlapping elsewhere (`exclusive=False` runs a job
  everywhere). Errors are logged and counted without stopping the schedule, a failing backend
  skips the tick unless `fail_open=True`, and `lifespan` / `async with` start and stop it with a
  `grace` period for running jobs.
- Circuit breakers: `FastAPIBreaker` (`fastapi_alcazaba.breaker`) for any dependency, as a
  decorator of async or sync functions or around a block (`attempt()`), with a `timeout` for
  async calls, `failures` and `exclude` to say what counts, and `BreakerOpen`, an
  `HTTPException` that FastAPI answers with a 503 and `Retry-After`.
- The `fastapi_alcazaba.breaker.calls`, `fastapi_alcazaba.breaker.transitions` and
  `fastapi_alcazaba.scheduler.runs` metrics.
- Locks, rate-limit counters, revoked tokens, one-time codes and idempotency records live under
  `{prefix}!` (`Cache.state_key`), apart from the cached responses: `clear()` never frees a held
  lock, resets a limit or takes a revocation back, and `InMemoryBackend` gives them a store of
  their own that caching responses never evicts.
- `Backend.invalidate_tags_with_keys`: an invalidation that also tells which keys it deleted.

### Changed

- `CircuitBreaker.allow()` returns the state the call goes through in (`"closed"`, or
  `"half-open"` for the single trial) or None when it's refused, instead of a bool; truthiness is
  the same.

### Fixed

- `TieredBackend`: a tag invalidation didn't reach the copies of processes that had only read the
  entry (they never saw its tags), not even in the process that invalidated, so they kept serving
  it for up to `local_ttl`. Invalidations now name the keys they deleted.
- `TieredBackend`: a value read from the shared backend, or written to it, while an invalidation
  arrived could be kept as the local copy although it was already the old one.
- The circuit breaker let a second trial through while the first one ran, when a call that wasn't
  the trial was cancelled.
- `TieredBackend` with a client that has a `socket_timeout`: the subscription to the invalidation
  channel ended every time the channel was quiet for that long, emptying the local level and
  logging a warning each time. The channel is now read with its own timeout and pinged when quiet;
  a connection that died silently is given up within 10 seconds, and a reconnection made by the
  client itself empties the local level (messages may have been missed).

## [0.1.0] - 2026-10-24

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
