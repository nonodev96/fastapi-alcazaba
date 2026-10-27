# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Fuzz targets (`fuzz/`) for what reads data from outside: the type and size sniffed from
  uploads and the safe file names, compressed entries, pagination cursors, signed webhooks and
  audit details, each checking the properties it must keep. ClusterFuzzLite runs them with
  atheris on pull requests and every week; the unit tests run them on random inputs.

- `fastapi_alcazaba.canonical.canonical_json`: the JSON Canonicalization Scheme (RFC 8785), the
  one serialization of a JSON value that every language implementing it writes the same.

### Changed

- Cached responses report the result in the `Cache-Status` header of RFC 9211 instead of
  `X-FastAPI-Cache`: `Alcazaba; hit; ttl=42` with the seconds the entry has left, or why the
  request reached the endpoint (`fwd=miss`, `stale` for an early refresh, `request` for a client's
  `no-cache`, `method`, `bypass` with the cache off), `stored` when its response was kept and
  `collapsed` when it waited for another request's. Responses that weren't cached get the header
  too. `FastAPICache.init(cache_status_header=...)` is now `cache_status=`, the cache's name
  (`None` leaves the header out), and `get_cache_status_header()` is `get_cache_status()`.
- The audit trail's signatures cover the event's RFC 8785 canonical JSON, after the context
  `fastapi_alcazaba/audit2` and a NUL byte, so that any language can check them; events signed
  by 0.2.0 no longer verify. Details keep to I-JSON for it: integers beyond 2⁵³ are recorded as
  text, and lone surrogates in any text of an event as U+FFFD.
- The API reference has a page per tool (cache, backends, limits, audit...) with an index of
  them, instead of one long page; each object is set apart, with its kind and its signature.

### Fixed

- `FastAPIPaginationCursor.decode()` raised a TypeError, instead of the 400 of an invalid cursor,
  for a signed cursor with non-ASCII characters (the route's own parameter never lets one in).
- `verify_audit_trail()` raised a TypeError for an event whose signature had non-ASCII
  characters, instead of reporting it as altered.
- `safe_filename()` with a `max_length` under 64 bytes could give a longer name when the
  extension was short in characters but long in bytes (emoji): such an extension is now dropped.

## [0.2.0] - 2026-10-26

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
- Problem details (`fastapi_alcazaba.problems`): `FastAPIProblemDetails().install(app)` answers
  HTTPExceptions, validation errors and unhandled exceptions with RFC 9457
  `application/problem+json` (`type`, `title`, `status`, `detail`, `instance` and the
  `request_id` of `RequestIDMiddleware`), keeping the exceptions' headers (`Retry-After`,
  `WWW-Authenticate`…). 5xx details are never sent (a hidden HTTPException detail is logged);
  4xx ones are unless `expose_detail=False`; validation errors list `loc`, `msg` and `type`, never
  FastAPI's `input` (which echoes passwords) nor `ctx`, at most `max_errors`. Unhandled exceptions
  get a bare 500 and are still raised again for the server to log. `Problem` is an HTTPException
  with a type URI, a title and checked extension members; `problems.response()` answers one from
  your own exception handlers.
- Request IDs (`fastapi_alcazaba.request_id`): `RequestIDMiddleware`, a pure ASGI middleware that
  gives every HTTP request and WebSocket an ID, reusing the incoming `X-Request-ID` only when it's
  a single value safe to log (`[A-Za-z0-9._:-]{1,128}`, `trust_incoming=False` to never reuse
  it) and making a random 128-bit one otherwise, and sends it back in the response. `request_id()`
  returns it while the request is handled (sync endpoints' threads included), and it stays set
  after an unhandled exception so that the 500 handler and the server's error log still know the
  request. `RequestIDLogFilter` puts it in log records as `%(request_id)s`.
- Request timeouts (`fastapi_alcazaba.timeouts`): `RequestTimeoutMiddleware`, a pure ASGI
  middleware that gives every HTTP request a deadline (`timeout=30` seconds, other ones by path
  prefix with `paths`, None for none) from its arrival until its response has been sent, reading
  the body included. At the deadline the app is cancelled (its `finally` blocks and `yield`
  dependencies' exits run) and the client gets a 504; a response already streaming is cut by
  raising the TimeoutError again, so that the server closes the connection instead of ending a
  short body as if it were complete. Background tasks after the response have no deadline;
  WebSockets and lifespan events go through untouched.
- Concurrency limits: `FastAPIConcurrencyLimiter` (`fastapi_alcazaba.concurrency`), a FastAPI
  dependency (with `yield`) that lets at most `limit` requests run at the same time across every
  process, per route, per `name` shared by routes, or per client (`identifier`, answered with a
  429 instead of a 503), with `Retry-After`. The semaphore is `limit` (up to 100) renewed locks in
  the cache's backend, tried in random order and expiring if their holder dies; requests wait up
  to `wait` seconds with jittered backoff. The slot is held until the response has been sent (the
  whole body of a `StreamingResponse` since FastAPI 0.118; `scope="function"` releases it
  earlier), and while a WebSocket is open. `slot()` takes one outside requests. When the backend
  fails, each process enforces the limit on its own (`fail_open=False` refuses with a 503).
- Leader election: `FastAPILeader` (`fastapi_alcazaba.leader`) elects one process among those
  sharing the cache's backend, on a renewed lock, and runs the async functions given to `run` (a
  decorator) while it leads: they're cancelled when the leadership is lost or the leader stops,
  and the process campaigns again. `start()`/`stop()`, `lifespan` and `async with`; `is_leader`
  works from sync code too. A stopping leader releases the lock for a quick hand-over; a dead one
  is replaced within `ttl` and a third. Nobody is elected while the backend fails.
- Maintenance mode: `FastAPIMaintenance` (`fastapi_alcazaba.maintenance`) with `enable(message,
  retry_after)`, `disable()` and `status()`, shared by every process through the cache's backend,
  and `MaintenanceMiddleware`, a pure ASGI middleware that answers 503 with `Retry-After` (and
  closes WebSockets with 1013) while it's on, except for `allow_paths` and the requests `allow`
  lets through. Each process keeps a copy of the state refreshed at most every `refresh` seconds,
  so requests don't reach the backend; when the backend fails, the last state read stays. The
  state is sealed with the cache's signing or encryption key when it has one.
- Feature flags: `FastAPIFeatureFlags` (`fastapi_alcazaba.flags`) stores flags in the cache's
  backend: `set(name, enabled=..., percentage=..., allow=..., deny=...)`, `delete`, `get`,
  `enabled(name, subject, default)` and `require(name, subject=...)`, a FastAPI dependency that
  answers 404 while the flag is off, without revealing it. Percentage rollouts are sticky (a hash
  of the flag and the subject) and only grow with the percentage. Without a percentage a flag is
  for everybody, or only for its allow list when it has one. Each process keeps definitions
  for `refresh` seconds and falls back on the last one read, or the default, when the backend
  fails. Subjects are stored as salted SHA-256 digests, never in clear, and definitions are
  sealed with the cache's signing or encryption key when it has one.
- API keys (`fastapi_alcazaba.apikeys`): `FastAPIAPIKeys(lookup)`, a dependency that
  authenticates machine clients by keys like `ak_<id>_<secret>` (80-bit id, 256-bit secret),
  read from `X-API-Key` or, with `bearer=True`, `Authorization: Bearer`, both declared in the
  OpenAPI schema. `generate()` returns the key, to show once, and an `APIKeyRecord` to store: a
  SHA-256 digest of the secret bound to its id, an HMAC with an optional `pepper` (rotation),
  scopes and an expiry. The app's own `lookup(key_id)` (async or sync, in a thread) returns its
  row, which the dependency returns after a constant-time comparison and the expiry check; every
  failure gets the same 401 with `WWW-Authenticate`, and `require(*scopes)` a 403. Keys are never
  logged. An optional per-process cache of records (`cache_ttl`, `cache_size`, `forget()`)
  trades revocation latency for fewer lookups.
- Signed webhooks (`fastapi_alcazaba.webhooks`), compatible with Standard Webhooks (Svix,
  Supabase, Clerk, Resend...): `WebhookSigner` signs outgoing ones (`whsec_` secrets, a random
  `webhook-id`, every secret of a rotation), and `FastAPIWebhooks`, a dependency, verifies
  incoming ones over the raw body, which the endpoint can still read (capped by `max_body_size`,
  413 beyond), with a timestamp `tolerance` both ways, constant-time comparison against every
  secret, and replay protection: each accepted delivery is counted in the cache's backend (hashed,
  under `state_key`) until it can't be accepted anymore, while the sender's retries (new timestamp
  and signature) still go through. Wrong signatures, stale timestamps and replays get the same 401;
  a failing backend a 503 (`fail_open` to accept instead). `verify()` checks a message without
  the replay check.
- SSRF protection for outbound calls: `SSRFGuardTransport` (`fastapi_alcazaba.outbound`), an httpx
  transport that resolves the host itself and refuses the request (`BlockedDestinationError`, an
  `httpx.RequestError` that `ResilientTransport` doesn't count against the origin) when any of its
  addresses isn't public: private, loopback, link-local and cloud metadata, carrier-grade NAT,
  multicast, reserved and documentation ranges, unique-local IPv6, and IPv4-mapped, NAT64, 6to4
  and Teredo forms. Literal addresses and numeric spellings are caught too. The connection is
  pinned to the checked address (URL rewritten to it, `Host` kept, SNI and certificate checked
  against the name), so DNS rebinding can't swap it; every redirect hop is checked; one address of
  the other family is tried if the first can't connect. `allow` lets internal names (`*.` for
  subdomains) and networks through, `ports` restricts ports, each host name gets its own
  connection pool (`max_hosts`), and proxied inner transports are refused. Composes as
  `ResilientTransport(SSRFGuardTransport())`.
- CSRF protection: `CSRFMiddleware` (`fastapi_alcazaba.csrf`), a plain ASGI middleware that
  refuses with a 403 the unsafe requests a page on another site made the browser send with the
  user's cookies or HTTP authentication. It trusts `Sec-Fetch-Site` (same-origin passes, same-site
  and cross-site only from `trusted_origins`), and else requires an `Origin` or `Referer` of the
  app's own origin (scheme and `Host` as the server sees them) or a trusted one; `Origin: null` and
  requests with neither are refused. Requests without cookies go through (token APIs aren't
  exposed), except form submissions from other sites (login CSRF); `check_all` checks everything.
  `exempt_paths` skips prefixes. `require_token` adds a double-submit token (a `__Host-` cookie
  compared in constant time with a header, also in `request.state.csrf_token`). WebSocket
  handshakes are checked too, against cross-site WebSocket hijacking.
- Server-side sessions: `ServerSessionMiddleware` (`fastapi_alcazaba.sessions`), a plain ASGI
  middleware that keeps each browser's session in the cache's backend and only a random 256-bit id
  in the cookie (`HttpOnly`, `Secure`, `SameSite=Lax`), so that, unlike Starlette's signed-cookie
  sessions, the client can't read it and logging out revokes it. `request.session` works as usual.
  The data is stored under a digest of the id, sealed with the cache's signing or encryption key,
  saved only when it changed (nested changes too), JSON-only with a size limit, and expires after
  `max_age` and `idle_timeout`. Ids are always generated by the server; `rotate_session` (at
  login, against session fixation) and `clear_session` (at logout) are included. A backend that
  can't be read gives an empty session; a change that can't be saved gives a 503.
- Upload checks with nothing but the standard library (`fastapi_alcazaba.uploads`):
  `FastAPIUploads(types, max_size=..., max_pixels=...)`, a dependency (or `check(file)`) that
  reads the upload in chunks, counting its size whatever `UploadFile.size` says (413), detects its
  real type from its bytes (415 when it isn't allowed), refuses images with more than
  `max_pixels` pixels (decompression bombs, 413) and returns a `CheckedFile` read back to its
  start, with the detected type, size, dimensions, extension and a safe name. `sniff(data)` tells
  PNG, JPEG, GIF, WebP, PDF, ZIP, GZIP, MP4, QuickTime, UTF-8 text, and Office documents (from the
  content types the archive declares, macro-enabled ones apart) from each other, and keeps HTML,
  SVG and XML apart from text; SVG is never an image. `image_size(data)` reads image dimensions
  from their headers, and `safe_filename(name)` makes a client's file name safe to display and to
  use on any file system.
- Task queue: `FastAPITaskQueue` (`fastapi_alcazaba.tasks`), on Redis 6.2+ or Valkey streams
  (clusters too). `@queue.task()` registers async or sync functions; `await queue.enqueue(task,
  *args, delay=...)` queues a run with JSON arguments checked against the function; the processes
  that `start()` it (or use `lifespan`) run them, `concurrency` at a time. Delivery is at least
  once: tasks are acknowledged when they finish, those of a worker that stopped answering are
  taken over after `visibility_timeout` (heartbeats keep long ones), and one handed out more than
  `max_deliveries` times is dead-lettered. Failures are retried with exponential backoff and
  jitter, then dead-lettered with the type of their error only (`dead_letters()`,
  `requeue_dead_letters()`, `discard_dead_letters()`). Only registered tasks run, never pickle;
  with the cache's signing or encryption key every task is sealed and bound to its queue, so
  planted or altered ones are refused and dead-lettered, never run. Delayed tasks move to the
  stream atomically, the queue is bounded (`QueueFull`) and payloads are size-capped. `stop()`
  gives running tasks `grace` seconds and puts back those it cancels.
- Telemetry for the tools: each run of a scheduled job, and of a task (linked to the span that
  enqueued it), is the root span of a trace of its own; rate-limit, breaker, outbound-breaker and
  CSRF refusals and breaker transitions are events on the request's span, and idempotency
  outcomes an attribute of it. New metrics: `fastapi_alcazaba.breaker.calls`,
  `.breaker.transitions`, `.scheduler.runs`, `.idempotency`, `.tokens.checks`,
  `.otp.verifications`, `.lock.wait`, `.lock.lost`, `.health.checks`, `.health.check.duration`,
  `.concurrency`, `.leader.transitions`, `.maintenance.refused`, `.flags.evaluations`,
  `.problems`, `.request_timeouts`, `.request_ids`, `.outbound.breaker`, `.outbound.ssrf.blocked`,
  `.webhooks`, `.api_keys`, `.csrf.refused`, `.sessions`, `.uploads`, `.uploads.size`,
  `.tasks.enqueued`, `.tasks`, `.tasks.duration` and `.tasks.reclaimed`. The README shows the recommended setup (OpenTelemetry's zero-code
  instrumentation and its standard variables): Alcazaba only uses the API.
- Locks, rate-limit counters, revoked tokens, one-time codes and idempotency records live under
  `{prefix}!` (`Cache.state_key`), apart from the cached responses: `clear()` never frees a held
  lock, resets a limit or takes a revocation back, and `InMemoryBackend` gives them a store of
  their own that caching responses never evicts.
- `Backend.invalidate_tags_with_keys`: an invalidation that also tells which keys it deleted.
- Examples for the tools (`examples/`): small, complete apps for the response cache, pagination,
  rate and concurrency limits, idempotency keys, API keys and one-time codes, a browser app with
  sessions and CSRF protection, uploads, webhooks, errors and observability, background work and
  operations. Each is tested with the library and has a page in the documentation's new Examples
  section, with how to run it and what it answers.
- A logo and a social card, and the documentation's theme in the project's colours.
- `RedisBackend.client` and `ValkeyBackend.client`: the client the backend talks through.
- pylint, configured in `pyproject.toml`, runs with ruff and mypy in `make lint`, pre-commit and CI.
- `FastAPIBreaker.reset()` closes a breaker by hand (the dependency is back, or between tests);
  `failure_threshold` and `reset_timeout` can be read back.
- `forget()` on `FastAPIMaintenance`, `FastAPIFeatureFlags` and `FastAPIRateLimiter` drops what
  each process keeps (its copies of the state, the clients it refuses without asking the
  backend). A new cache drops it by itself, as each test's lifespan sets one up.
- `SSRFGuardTransport.check(url)`: checks a URL as a request to it would be, without sending
  anything, to refuse a webhook's endpoint when it's registered.
- With `FastAPIProblemDetails` installed, Alcazaba's middlewares answer with problems too (the 504
  of a timeout, the 413 of a body too large, the 503 of maintenance, the 403 of CSRF...), and the
  error responses keep the rate limiters' `RateLimit` headers.
- `scripts/check_signatures.py`, in `make lint`, `make format`, pre-commit and CI: a signature
  that doesn't fit on one line has one parameter per line, the last one with a comma.
- An audit trail (`FastAPIAudit`, `fastapi_alcazaba.audit`): who did what to which resource,
  whether it worked, and the request it came from. `audit.action()` is a dependency that writes
  the event when the endpoint ends (`success`, `denied` for a 401 or a 403, `failure` with the
  exception's type and status, never its message), the refusals of the authentication included;
  `audit.log()` and `audit.record()` work outside endpoints. Events carry the request ID, the
  trace, the route template (never the path) and the client's address; their details keep no
  secrets (values under keys such as `password` or `token`, and credentials, are redacted) and
  are bounded. They go to the `fastapi_alcazaba.audit.events` logger as JSON lines, which warns
  when it outputs nothing, or to sinks of your own. With a `signing_key` they're signed and
  chained, and `verify_audit_trail()` finds the ones altered, forged or removed. A sink that fails
  is logged with the event and counted, and an event is written even if its request is cancelled.
  The `fastapi_alcazaba.audit` and `fastapi_alcazaba.audit.undelivered` metrics, and an example.
- Every parameter that takes a key or a secret (`signing_key`, `encryption_key`, `pepper`, the
  secrets of one-time codes, cursors and webhooks...) also takes pydantic's `SecretStr` or
  `SecretBytes`, as pydantic-settings loads them, so the value stays out of the settings' repr
  until it's used. The examples read their configuration with pydantic-settings, from the
  environment or a `.env` file.

### Changed

- `CircuitBreaker.allow()` returns the state the call goes through in (`"closed"`, or
  `"half-open"` for the single trial) or None when it's refused, instead of a bool; truthiness is
  the same.
- Development, CI and releases use Python's own `venv` and pip (26.2 or later) instead of uv.
  `make deps` sets up `.venv`; the dependency groups of `pyproject.toml` install with pip's
  `--group`; pip's `--uploaded-prior-to P3D` keeps releases younger than three days out; the SBOM
  comes from pip's installation report, with the hashes of the files. There's no lock file: CI
  installs the latest releases that are at least three days old.
- A backend asked for something it can't do (locks, tags, counters) raises a NotImplementedError
  that says so, e.g. `MemcachedBackend can't hold locks`, instead of one without a message.

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
- `DynamoBackend.clear(key=...)`: of several deletes of one entry at the same time, more than one
  could be told that it was there. The delete is now conditional, so exactly one is, and a
  one-time code works only once on DynamoDB too.
- `Lock.release()` (and leaving `async with cache.lock(...)`) could wait forever when the
  client swallowed the cancellation of a renewal under way (fakeredis does on Python 3.11): the
  renewals now stop as soon as the lock is released.

## [0.1.0] - 2026-10-24

First release of Alcazaba.

### Security

- Per-user cache entries for requests with `Authorization` or `Cookie` (`vary_headers`), sent as
  `Cache-Control: private` with `Vary`: one user's response is never served to another.
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

- Cache hits return the annotated return type (Pydantic models, dataclasses).
- Deterministic, quoted ETags; `If-None-Match` lists, weak tags and `*`.
- The response model appears in OpenAPI.
- No `Cache-Control: max-age=None`; headers also reach `Response`s returned by the endpoint.
- `FastAPICache.clear(key=...)` with a prefix no longer clears the whole prefix.
- Entries that can't be decoded are recomputed instead of failing.
- Importing works without jinja2 (Starlette 1.x).
- In-memory entries without `expire` no longer expire immediately; memcached expirations over 30
  days; the settings of one call no longer leak into the next.

### Changed

- Distribution `fastapi-alcazaba`, import package `fastapi_alcazaba`. Python 3.11+ (3.10 reached its end
  of life in October 2026), Pydantic v2.
- `redis` is optional; `uvicorn` and `pendulum` are no longer dependencies.
