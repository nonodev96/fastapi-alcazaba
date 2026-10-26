# Alcazaba

**Secure, maintained tools for FastAPI: response cache, rate limiter and more.**

Alcazaba (`fastapi-alcazaba`) gathers the pieces most FastAPI apps end up writing themselves,
built with security in mind and tested against real servers:

| Tool | What it does |
| --- | --- |
| [Response cache](#quick-start) | `@cache` for endpoints and functions, in memory, Redis, Valkey, memcached or DynamoDB |
| [Rate limiter](#rate-limiting) | `FastAPIRateLimiter`: a dependency with a sliding window shared by every process |
| [Locks](#locks) | `async with cache.lock("job")`: one process at a time, renewed while held |
| [Idempotency keys](#idempotency-keys) | a retried POST gets the first response instead of running twice |
| [Revoked tokens and one-time codes](#revoked-tokens-and-one-time-codes) | a JWT blocklist and e-mail/SMS codes, nothing stored in clear |
| [API keys](#api-keys) | keys stored only as digests, with scopes and expiry |
| [Signed webhooks](#signed-webhooks) | Standard Webhooks signing and verification, replays refused |
| [Outbound calls](#outbound-calls) | an httpx transport with a deadline per call, a circuit breaker per origin and SSRF protection |
| [Security headers and body size](#security-headers-and-body-size) | the OWASP headers, and a 413 for endless bodies |
| [CSRF protection](#csrf-protection) | `CSRFMiddleware`: a 403 for what another site makes the browser send with its cookies |
| [Server-side sessions](#server-side-sessions) | `request.session` kept in the backend, only a random id in the cookie: revocable |
| [Checking uploads](#checking-uploads) | `FastAPIUploads`: the real type from the bytes, size, image dimensions, safe names |
| [Problem details](#problem-details) | errors as RFC 9457 `application/problem+json`, never leaking internals |
| [Request IDs](#request-ids) | an ID per request in logs, responses and errors |
| [Request timeouts](#request-timeouts) | a deadline per request: a 504 instead of a request hung forever |
| [Health checks](#health-checks) | `/health/live` and `/health/ready` for load balancers and orchestrators |
| [Scheduled jobs](#scheduled-jobs) | `FastAPIScheduler`: every N seconds or by cron, each tick once across processes |
| [Task queue](#task-queue) | `FastAPITaskQueue`: background tasks on Redis streams, retried, at least once, never pickle |
| [Circuit breakers](#circuit-breakers) | `FastAPIBreaker`: stop calling a dependency that keeps failing, a 503 meanwhile |
| [Concurrency limits](#concurrency-limits) | `FastAPIConcurrencyLimiter`: at most N requests at a time, across processes |
| [Leader election](#leader-election) | `FastAPILeader`: work that must run in exactly one process at a time |
| [Maintenance mode](#maintenance-mode) | `FastAPIMaintenance`: a 503 with `Retry-After` for every process at once |
| [Feature flags](#feature-flags) | `FastAPIFeatureFlags`: on, off, by percentage or by subject, shared by every process |
| [Pagination](#pagination) | `FastAPIPagination`, `…LimitOffset` and `…Cursor`, each page cached under its own key |
| [Invalidation by tag](#invalidating-by-tag) | drop every entry of a product, a user… without knowing their keys |
| [Two-level cache](#two-level-cache) | a copy in each process in front of Redis, kept in sync by pub/sub |
| [Metrics and traces](#metrics-and-traces) | OpenTelemetry for all of the above |

It started as a fork of [fastapi-cache](https://github.com/long2ice/fastapi-cache)
(`fastapi-cache2` on PyPI), whose last release dates from July 2024. The cache keeps the same API
— you change the imports — and fixes the security problems of the original:

- **Users never get each other's responses.** Requests carrying `Authorization` or `Cookie` get
  their own cache entries and the response is sent as `Cache-Control: private`. fastapi-cache2
  served one user's response to the next when the endpoint authenticated through a router
  dependency.
- **No Redis script injection.** `FastAPICache.clear(namespace=...)` pasted the namespace into a
  Lua script that ran `KEYS`: a namespace from user input could run any command, and `KEYS`
  blocks the server. Alcazaba uses `SCAN` + `UNLINK`.
- **Signed or encrypted entries.** With a `signing_key`, every entry carries an HMAC bound to its
  key: whoever can write to Redis can't plant data, move one user's entry under another user's
  key, or get a pickle executed. With an `encryption_key` they're also encrypted (AES-256-GCM): a
  dump or a backup of Redis doesn't reveal the cached responses.
- **Bounded memory.** `InMemoryBackend` evicts the least recently used entries instead of
  growing forever with every new query string.
- **Clients can't bypass the cache** with `Cache-Control: no-cache` unless you allow it.
- **Only successful responses are cached**, never a 404 or a 500 replayed as a 200.
- **Cache stampede protection**: concurrent misses for one key compute once, in each process
  and, with `distributed_lock`, across processes.
- **A broken backend doesn't take the app down**: every backend call has a timeout, and a circuit
  breaker stops calling it while it fails.
- **Keys that never repeat are reported**: an argument whose `repr()` changes on every call (a
  database session, a service) makes every request a miss; Alcazaba warns about it.

And more: early refresh of popular entries, Valkey through valkey-py or Valkey GLIDE, and the
fixes that waited in upstream pull requests: cache hits
returning Pydantic models again (not dicts), deterministic `ETag`s, the response model back in
OpenAPI, `exclude_params`, `stale-while-revalidate`, Starlette 1.x without jinja2… See
[UPSTREAM.md](https://github.com/nonodev96/fastapi-alcazaba/blob/main/UPSTREAM.md) for what happened to every upstream pull request.

## Install

```shell
pip install fastapi-alcazaba            # in-memory backend only
pip install "fastapi-alcazaba[redis]"   # or [valkey], [glide], [memcache], [dynamodb], [all]
pip install "fastapi-alcazaba[otel]"    # OpenTelemetry metrics
pip install "fastapi-alcazaba[crypto]"  # encrypted entries
pip install "fastapi-alcazaba[zstd]"    # zstd compression before Python 3.14
```

Python 3.11+, FastAPI 0.115+ and Pydantic v2.

## Quick start

```python
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header
from pydantic import BaseModel
from redis import asyncio as aioredis

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.decorator import cache


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    redis = aioredis.from_url(os.environ["REDIS_URL"])
    FastAPICache.init(
        RedisBackend(redis),
        prefix="myapp",
        signing_key=os.environ["CACHE_SIGNING_KEY"],  # secrets.token_urlsafe(32)
    )
    yield
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


class Product(BaseModel):
    name: str
    price: float


def current_user(authorization: str = Header()) -> str:
    return authorization  # validate the token here


@app.get("/products/{product_id}")
@cache(expire=60, vary_headers=())  # same response for everybody: one shared entry
async def product(product_id: int) -> Product:
    return Product(name="Olive oil", price=9.5)


@app.get("/me")
@cache(expire=30)  # one entry per Authorization/Cookie value, sent as private
async def me(user: str = Depends(current_user)) -> dict[str, str]:
    return {"user": user}
```

`@cache` goes between the route decorator and the function. It also caches plain functions,
sync or async (sync ones run in the thread pool, like FastAPI does).

## Migrating from fastapi-cache2

1. `pip uninstall fastapi-cache2 && pip install fastapi-alcazaba`
2. Replace `fastapi_cache` with `fastapi_alcazaba` in the imports. Class and function names don't change.
3. Review these behaviour changes (all of them on purpose):

| fastapi-cache2 | Alcazaba | To get the old behaviour |
| --- | --- | --- |
| One entry for all users | One per `Authorization`/`Cookie` value, `private` | `vary_headers=()` on public endpoints |
| `Cache-Control: no-cache`/`no-store` from the client skips the cache | Ignored | `respect_cache_control=True` |
| Error `Response`s cached and replayed as 200 | Not cached | — |
| Cache hits return dicts (since 0.2.2) | Return the annotated type | — |
| MD5 keys | SHA-256 keys: existing entries are not reused | — |
| `InMemoryBackend` unbounded, shared by all instances | 10 000 entries per instance (LRU) | `InMemoryBackend(max_size=None)` |
| Injected parameters `__fastapi_cache_request/_response` | `__fastapi_alcazaba_request/_response` | `injected_dependency_namespace="__fastapi_cache"` |
| `MemcachedBackend.clear(namespace=...)` unsupported | Still unsupported (flushing would wipe other apps) | — |
| A hanging backend makes every request wait | 1 s timeout per call and a circuit breaker | `backend_timeout=None, circuit_breaker_failures=None` |
| Python 3.8+ | Python 3.11+ (3.10 reached its end of life in October 2026) | — |

The response header saying `HIT`/`MISS` is still `X-FastAPI-Cache`.

## Configuration

### `FastAPICache.init()`

Call it once, at startup (in the `lifespan`).

| Parameter | Default | Description |
| --- | --- | --- |
| `backend` | | `InMemoryBackend`, `RedisBackend`, `ValkeyBackend`, `GlideBackend`, `MemcachedBackend` or `DynamoBackend` |
| `prefix` | `""` | prefix of every key |
| `expire` | `None` | default time to live, in seconds |
| `coder` | `JsonCoder` | how values are stored |
| `key_builder` | `default_key_builder` | builds keys from the function and its arguments |
| `cache_status_header` | `"X-FastAPI-Cache"` | header saying `HIT` or `MISS`; `""` to leave it out |
| `enable` | `True` | `False` turns the cache off |
| `signing_key` | `None` | secret of 32+ bytes to sign entries; a list rotates keys (the first signs) |
| `encryption_key` | `None` | secret of 32+ bytes to sign *and encrypt* entries (`crypto` extra); instead of `signing_key` |
| `compression` | `None` | `"zstd"` or `"zlib"`: store large entries compressed |
| `compression_min_size` | `1024` | bytes; smaller entries aren't compressed |
| `compression_level` | codec's default | |
| `vary_headers` | `("authorization", "cookie")` | request headers that give each value its own entry |
| `respect_cache_control` | `False` | let clients skip the cache with `no-cache`/`no-store` |
| `single_flight` | `True` | concurrent misses for one key compute once, per process |
| `backend_timeout` | `1.0` | seconds a backend call may take before the request goes on without cache |
| `circuit_breaker_failures` | `5` | consecutive failures that stop the backend calls; `None` disables the breaker |
| `circuit_breaker_reset` | `30.0` | seconds before trying the backend again |
| `max_value_size` | `None` | bytes; larger values aren't stored |
| `distributed_lock` | `False` | one process computes a missing value while the others wait for it |
| `lock_timeout` | `10.0` | seconds before a lock expires (if its holder dies) |
| `lock_wait` | `5.0` | seconds a process waits for another one's value before computing it |
| `early_refresh` | `None` | fraction of the lifetime, at its end, in which hits may refresh the entry |
| `metrics` | `True` | OpenTelemetry metrics, with the `otel` extra |
| `meter_provider` | global | OpenTelemetry meter provider |
| `tracing` | `True` | OpenTelemetry spans, with the `otel` extra |
| `tracer_provider` | global | OpenTelemetry tracer provider |

### `@cache()`

| Parameter | Default | Description |
| --- | --- | --- |
| `expire` | from `init` | time to live, in seconds |
| `coder` | from `init` | |
| `key_builder` | from `init` | |
| `namespace` | `""` | group of entries, for `FastAPICache.clear(namespace=...)` |
| `injected_dependency_namespace` | `"__fastapi_alcazaba"` | prefix of the `Request`/`Response` parameters added to the endpoint |
| `exclude_params` | `None` | arguments left out of the key (a nonce, a database session…) |
| `vary_headers` | from `init` | `()` for responses that are the same for everybody |
| `respect_cache_control` | from `init` | |
| `stale_while_revalidate` | `None` | adds that `Cache-Control` directive |
| `single_flight` | from `init` | |
| `max_value_size` | from `init` | |
| `distributed_lock` | from `init` | |
| `early_refresh` | from `init` | |
| `tags` | `None` | names to invalidate the entry by: a list, or a function of the arguments |

Delete entries with `await FastAPICache.clear(namespace="products")`, one entry with
`await FastAPICache.clear(key=...)`, or by tag with `await FastAPICache.invalidate_tags(...)`.
`clear()` without arguments deletes every cached response under the prefix. Locks, rate-limit
counters, revoked tokens, one-time codes and idempotency records are kept apart, under
`{prefix}!` instead of `{prefix}:`, so no `clear()` frees a held lock, resets a limit or takes a
revocation back; `InMemoryBackend` keeps them apart too, so caching many responses never evicts
them.

## Several caches, dependency injection and `get_or_set`

`FastAPICache` is one global cache. For one cache per app, or several with different settings,
create `Cache` instances (same arguments as `FastAPICache.init()`) and decorate with their
`cached()`:

```python
from fastapi_alcazaba import Cache, get_cache

public_cache = Cache(RedisBackend(redis), prefix="public", vary_headers=())
private_cache = Cache(
    RedisBackend(redis), prefix="private", encryption_key=os.environ["CACHE_ENCRYPTION_KEY"]
)
private_cache.install(app)  # what get_cache returns


@app.get("/catalog")
@public_cache.cached(expire=600)
async def catalog() -> list[Product]: ...


@app.get("/me/orders")
async def orders(
    user: Annotated[User, Depends(current_user)], cache: Annotated[Cache, Depends(get_cache)]
) -> list[Order]:
    # Anything worth caching that doesn't fit a decorator; the key is hashed before storing.
    return await cache.get_or_set(f"orders:{user.id}", lambda: load_orders(user.id), expire=60)
```

`get_or_set` goes through the same pipeline as the decorator (signing or encryption,
compression, single flight, locks, tags). Close the backend at shutdown with `await
cache.close()`.

## Invalidating by tag

Tag entries when they're cached and delete them all when the data changes, without knowing their
keys:

```python
@app.get("/products/{product_id}")
@cache(expire=600, tags=lambda product_id, **_: [f"product:{product_id}", "catalog"])
async def product(product_id: int) -> Product: ...


@app.put("/products/{product_id}")
async def update_product(product_id: int, data: Product) -> Product:
    saved = save(product_id, data)
    await FastAPICache.invalidate_tags(f"product:{product_id}")
    return saved
```

The function receives the same arguments as the endpoint. Tags work with the in-memory, Redis,
Valkey and GLIDE backends; in Redis-like servers each tag is a set that lives as long as its
longest-lived entry.

## Rate limiting

`FastAPIRateLimiter` is a FastAPI dependency that lets each client make `times` requests per
window to a route; the next ones get a `429 Too Many Requests` with `Retry-After`. Its counters
live in the cache's backend, so every process and container shares them:

```python
from functools import partial

from fastapi import APIRouter, Depends
from starlette.requests import HTTPConnection

from fastapi_alcazaba.limiter import FastAPIRateLimiter


@app.get("/search", dependencies=[Depends(FastAPIRateLimiter(times=10, minutes=1))])
async def search(q: str) -> list[Product]: ...


def user_id(connection: HTTPConnection) -> str:  # a Request, or a WebSocket
    return connection.state.user_id  # set by your authentication middleware


# 1000 requests per hour and user, for all the routes of the router together.
api = APIRouter(
    dependencies=[Depends(FastAPIRateLimiter(1000, hours=1, name="api", identifier=user_id))]
)

# One budget across routes, of which each export takes 50.
partner_budget = partial(FastAPIRateLimiter, 1000, hours=1, name="partners", identifier=user_id)
partners = APIRouter()
partners.get("/orders", dependencies=[Depends(partner_budget())])(list_orders)
partners.get("/export", dependencies=[Depends(partner_budget(cost=50))])(export_orders)
```

- **Who:** by default, the client's address as the ASGI server reports it (`client_ip`), IPv6
  addresses by their /64 network: each customer gets at least a whole /64, and one budget per
  address would give a client 2⁶⁴ of them. Many ISPs delegate a /56 or a /48, and tunnel brokers
  hand out /48s for free: `client_network(24, 48)` groups wider networks. Behind
  a proxy, let the server take the address from the proxy's headers (`uvicorn --proxy-headers
  --forwarded-allow-ips=<proxy address>`): the limiter never reads `X-Forwarded-For` itself, which
  any client can set. `identifier=` counts per user, API key… Identities are hashed before they
  reach the backend.
- **Where:** each route has its own budget, whatever its path parameters: `/items/1` and
  `/items/2` share one. Limiters with the same `name` and rate share one budget across routes,
  and `cost=` makes a route take more of it.
- **How:** a sliding window. The count of the previous window is added in proportion to how much
  of it is still less than a window ago, so nobody doubles the limit around the boundary between
  two windows. Requests over the limit don't count: checking and counting is one atomic
  operation (a Lua script on Redis, Valkey and GLIDE, a conditional write on DynamoDB). A client
  with nothing left is then refused by each process without asking the backend, until something
  fits again: a flood doesn't reach the backend. Windows follow the clocks of the app's servers:
  keep them in sync (NTP).
- **What clients see:** responses carry the `RateLimit` and `RateLimit-Policy` headers of
  the [IETF draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/):
  `RateLimit: "api";r=998;t=3540` means 998 requests left, at least for the next 3540 seconds.
  Clients can slow down before they get a 429. `headers=False` leaves them out. An endpoint that
  returns a `Response` object itself gets none: FastAPI only adds the headers that dependencies
  set to the responses it builds from a return value.
- **Which backend:** the `cache=` given, else the app's (`Cache.install`) or `FastAPICache`'s. Every
  backend can keep counters; `TieredBackend` keeps them only in its shared backend, and
  `FastAPICache.clear()` never resets them. They go through the cache's timeout and circuit
  breaker: if the backend fails, requests go through (`fail_open=False` answers 503 instead).
- **What happens over the limit:** `callback(request, seconds_to_wait)`, by default
  `too_many_requests` (the 429). If the callback returns, the request goes on.

On a WebSocket route, the dependency limits connections (closing with 1008). Call
`await limiter.hit(connection, key=..., cost=...)` yourself to limit anything else: it counts and
returns `None` within the limit, else the seconds to wait. For example, the messages of a WebSocket, or
the login attempts to an account:

```python
from fastapi_alcazaba.limiter import FastAPIRateLimiter, too_many_requests

login_attempts = FastAPIRateLimiter(times=5, minutes=15, identifier=lambda _: "")


@app.post("/login")
async def login(request: Request, form: Annotated[LoginForm, Form()]) -> Token:
    # Five attempts per account every 15 minutes, wherever they come from.
    if (wait := await login_attempts.hit(request, key=form.username)) is not None:
        too_many_requests(request, wait)
    ...
```

### Migrating from fastapi-limiter

| fastapi-limiter | Alcazaba |
| --- | --- |
| `await FastAPILimiter.init(redis)` | The cache's backend: `FastAPICache.init(...)` or `Cache(...).install(app)` |
| `RateLimiter(times=2, seconds=5)` (0.1), `RateLimiter(limiter=Limiter(Rate(2, Duration.SECOND * 5)))` (0.2) | `FastAPIRateLimiter(times=2, seconds=5)` |
| The client address from `X-Forwarded-For`: a new one, a new budget | From the ASGI server, which you tell which proxies to trust |
| A budget per URL: `/items/1`, `/items/2`… | Per route, or per `name` across routes |
| `callback(request, response, pexpire)` | `callback(request, seconds_to_wait)` |
| `WebSocketRateLimiter(...)(websocket, context_key)` | `await FastAPIRateLimiter(...).hit(websocket, key=...)` |
| `@skip_limiter` | Put the limiter only on the routes or routers that need it |
| No headers for clients | `RateLimit` and `RateLimit-Policy` on the responses |

## Locks

A lock shared by every process and container, for work that only one of them must do at a time:

```python
from fastapi_alcazaba.locks import LockNotAcquired


async def nightly_report() -> None:  # every instance runs it; one builds the report
    try:
        async with FastAPICache.lock("nightly-report", ttl=60):  # or cache.lock(...)
            await build_report()
    except LockNotAcquired:
        pass  # another instance is on it
```

- **Expiry and renewal:** a lock expires after `ttl` seconds, so a holder that dies doesn't keep it
  forever; while it's held, a background task renews it every `ttl / 3`.
- **Only the holder** releases or renews it: each acquisition has a random token, checked by a Lua
  script on Redis, Valkey and GLIDE and by conditional writes on DynamoDB (in memory, within one
  process). memcached can't hold locks. On DynamoDB, expiry compares the clocks of the app's
  servers: keep them in sync (NTP). A custom backend written for 0.1 has no `extend_lock`: use
  `renew=False` there, with a `ttl` longer than the work.
- **Waiting:** `wait=0` (the default) tries once, `wait=5` keeps trying for 5 seconds,
  `wait=None` as long as it takes. `acquire()` and `release()` work without `async with`.
- **Failures:** if the backend fails, `acquire()` raises `BackendUnavailable`: without it nobody can
  tell who holds the lock.
- **Limits:** a holder that stalls for longer than `ttl` (a long pause, a network partition) loses
  the lock while it still runs. `lock.held` turns False when the renewals couldn't keep up: check it
  between the steps of long jobs, and make work that must never overlap idempotent too.

## Idempotency keys

A client that retries a POST after a timeout or a dropped connection gets the first response
again, instead of creating a second order or paying twice
([draft-ietf-httpapi-idempotency-key-header](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)):

```python
from fastapi_alcazaba.idempotency import IdempotencyMiddleware, require_idempotency_key

app.add_middleware(IdempotencyMiddleware)  # the app's cache (Cache.install) or FastAPICache's


@app.post("/payments", status_code=201, dependencies=[Depends(require_idempotency_key)])
async def pay(payment: Payment) -> Receipt: ...
```

The client sends a fresh key with each new request, and the same one when it retries it:
`Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"`.

- **Retries get the first response** (status, headers and body) with `Idempotent-Replayed: true`;
  the endpoint runs once.
- **One at a time:** a retry that arrives while the first request still runs gets a 409 with
  `Retry-After: 1`. A lock in the backend, renewed while the request runs, sees to it across
  processes.
- **The same key for another request** (method, path, query or body) gets a 422.
- **Per client:** keys are looked up among the requests with the same `Authorization` header
  (hashed), so one user never gets another's response. With cookie sessions, and with access
  tokens that get refreshed (a retry sent with a fresh token would run again), pass
  `identifier=` returning the user's id. Requests without credentials share one space of keys,
  safe as long as clients make their keys random (UUIDs).
- **What's kept, and for how long:** responses below 500 for `expire` seconds (24 hours), except
  those worth retrying (408, 409, 425, 429) and those about the request itself, given before the
  endpoint did anything (401, 403, 404, 405, 413, 415, 422 and the like): a retry gets them
  afresh, and nobody can fill the backend with them (FastAPI's 422 echoes the request body).
  Request bodies are read whole to fingerprint them, up to `max_body_size` (a 413 beyond); a
  client that goes away before sending all of it doesn't reach the endpoint. Responses above
  `max_response_size` aren't kept, so a retry runs again. The stored responses go through the
  cache's signing or encryption: use an `encryption_key` when they carry personal data.
- **Failures:** if the backend fails the request gets a 503: running a payment twice is worse
  than refusing it. `fail_open=True` runs it without idempotency instead. Errors of the endpoint
  itself, `BackendUnavailable` included, are left to the app.
- Requests without the header go through untouched. It applies to POST and PATCH (`methods=`),
  and needs a backend with locks.

## Revoked tokens and one-time codes

A JWT can't be taken back once issued. `TokenBlocklist` keeps the revoked ones in the cache's
backend, shared by every process, until they would have expired anyway:

```python
from fastapi_alcazaba.tokens import OneTimeCodes, TokenBlocklist

blocklist = TokenBlocklist()  # FastAPICache's backend; or TokenBlocklist(cache)


async def current_user(claims: Annotated[dict[str, Any], Depends(decode_jwt)]) -> str:
    sub, iat = claims["sub"], claims["iat"]
    if await blocklist.is_revoked(claims["jti"], subject=sub, issued_at=iat):
        raise HTTPException(401)
    return sub


@app.post("/logout")
async def logout(claims: Annotated[dict[str, Any], Depends(decode_jwt)]) -> None:
    await blocklist.revoke(claims["jti"], expires_at=claims["exp"])


@app.post("/password")
async def change_password(user: Annotated[str, Depends(current_user)]) -> Token:
    ...
    await blocklist.revoke_subject(user, ttl=TOKEN_LIFETIME)  # log out everywhere
    return issue_token(user)  # issued now: still valid
```

`OneTimeCodes` are the codes you send by e-mail or SMS:

```python
codes = OneTimeCodes(secret=os.environ["OTP_SECRET"])  # secrets.token_urlsafe(32)


@app.post("/login/code", dependencies=[Depends(FastAPIRateLimiter(times=3, minutes=15))])
async def send_code(email: str) -> None:
    await send_email(email, await codes.issue(email, purpose="login"))


@app.post("/login")
async def login(email: str, code: str) -> Token:
    if not await codes.verify(email, code, purpose="login"):
        raise HTTPException(400, "Invalid or expired code")
    ...
```

- **Nothing in clear:** token ids and subjects are stored as SHA-256 digests. Codes are stored as
  an HMAC with `secret`, bound to their subject and purpose: a plain digest of a 6-digit code is
  brute-forced in milliseconds by whoever reads the backend. A list of secrets rotates them.
- **Expiry:** each revocation lasts while the token could still be accepted (`exp`, plus
  `leeway`, 60 s by default, for what your JWT library tolerates). Codes last `ttl` seconds (300).
- **Log out everywhere:** `revoke_subject` refuses the tokens issued before the current second.
  `iat` has whole seconds, so the token you issue right after for the current session stays
  valid.
- **Guesses and single use:** every guess counts, malformed ones included, and a code is lost
  after `max_attempts` (5). Of several requests with the right code at the same time, only one
  succeeds. Issuing a code replaces the previous one and gives the attempts again: rate-limit the
  route that issues them.
- **Every process sees them at once:** they're read from the shared backend, never from
  `TieredBackend`'s copies in each process. On DynamoDB, whose reads are eventually consistent, a
  revocation or a new code can take about a second to reach every process.
- **Failures:** if the backend fails, `is_revoked` refuses tokens (`fail_open=True` accepts them),
  `verify` returns False, and `revoke`, `revoke_subject` and `issue` raise `BackendUnavailable`.
  With a `signing_key` or `encryption_key`, revocations are sealed and planted ones are ignored.
  Whoever can delete keys in the backend can still un-revoke tokens.

## API keys

`FastAPIAPIKeys` authenticates machine clients by an API key. It generates the keys and checks
them; your database keeps a record of each, never the key itself:

```python
from fastapi_alcazaba.apikeys import FastAPIAPIKeys


async def load_key(key_id: str) -> ApiKeyRow | None:  # any object with digest, scopes, expires_at
    return await db.get(ApiKeyRow, key_id)


keys = FastAPIAPIKeys(load_key, pepper=os.environ["API_KEY_PEPPER"], bearer=True)


@app.post("/api-keys")
async def create_key(user: Annotated[User, Depends(current_user)]) -> dict[str, str]:
    new = keys.generate(scopes=["orders:read"], expires_at=datetime.now(UTC) + timedelta(days=90))
    await db.add(ApiKeyRow(owner=user.id, **dataclasses.asdict(new.record)))
    return {"key": new.key}  # shown once: only its digest is stored


@app.get("/orders")
async def orders(key: Annotated[ApiKeyRow, Depends(keys.require("orders:read"))]) -> list[Order]:
    return await db.orders(owner=key.owner)
```

- **Key format:** `ak_<id>_<secret>` (`prefix` changes `ak`; `sk_live` works too): an 80-bit id
  that finds the record and a 256-bit secret, in lowercase base32. A distinctive prefix lets
  secret scanners (GitHub's, GitLab's) spot leaked keys.
- **Stored as a digest:** the record holds a SHA-256 digest of the secret, bound to the key's id.
  Random 256-bit secrets don't need a slow password hash. With a `pepper` (a server secret of at
  least 32 bytes, kept out of the database) the digest is an HMAC, so a copy of the database
  alone can't even test a guess. A list of peppers rotates them: the first one makes new keys,
  all of them are accepted.
- **Your database:** `lookup(key_id)` returns your row, async or not (a plain function runs in a
  thread, like FastAPI's sync dependencies), and the dependency returns that same row. Any object
  with `digest`, `scopes` (a collection, or a space-separated string) and `expires_at` works;
  `APIKeyRecord` is what `generate()` gives.
- **Where the key comes from:** the `X-API-Key` header (`header=...`) and, with `bearer=True`,
  `Authorization: Bearer <key>`. Both appear in the OpenAPI schema, for the docs' Authorize
  button.
- **One answer:** a missing, malformed, unknown, wrong or expired key gets the same 401, with
  `WWW-Authenticate`; the secret is compared in constant time, and malformed keys don't reach
  `lookup`. `keys.require("a", "b")` adds a 403 for a valid key without all those scopes (with an
  RFC 6750 `insufficient_scope` challenge in bearer mode). Keys and secrets are never logged.
- **Revocation:** delete the record, or set its `expires_at` in the past (naive datetimes are
  taken as UTC). `cache_ttl` keeps looked-up records in each process for that many seconds to
  spare the database, so a revocation then takes up to `cache_ttl` to reach every process;
  `keys.forget(key_id)` drops one from this process at once. Unknown ids are never cached.
- **Floods:** each request with a well-formed key costs a lookup: put a `FastAPIRateLimiter`
  in front.
- **Without a dependency:** `await keys.authenticate(key)` for keys that come some other way (a
  WebSocket message).

## Signed webhooks

`FastAPIWebhooks` accepts only webhooks signed with your secret, and each delivery once.
`WebhookSigner` signs the ones you send. Both follow
[Standard Webhooks](https://www.standardwebhooks.com), the format of Svix, Supabase, Clerk,
Resend, OpenAI and others: `webhook-id`, `webhook-timestamp` and
`webhook-signature: v1,<base64 HMAC-SHA256 of "id.timestamp.body">`, with `whsec_...` secrets.

```python
from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookMessage, WebhookSigner

webhooks = FastAPIWebhooks(os.environ["WEBHOOK_SECRET"])  # "whsec_..."


@app.post("/webhooks/payments")
async def payment_event(message: Annotated[WebhookMessage, Depends(webhooks)]) -> None:
    event = message.json()
    ...  # idempotent on message.id: retries of a message keep its id


# Sending them: sign the exact bytes you send.
signer = WebhookSigner(os.environ["WEBHOOK_SECRET"])  # WebhookSigner.generate_secret() makes one
body = json.dumps(event).encode()
await client.post(url, content=body, headers=signer.sign(body))
```

- **Raw body, still readable:** the signature is checked over the body exactly as it arrived,
  read up to `max_body_size` bytes (1 MiB; a 413 beyond, and a `Content-Length` over it is
  refused before reading). It stays in the request: the endpoint can still take a pydantic model,
  `await request.json()` or `message.json()`. Not a form parameter, which consumes the body
  first.
- **Freshness:** a timestamp more than `tolerance` seconds (300) away from the server's clock,
  in the past or the future, is refused.
- **Constant time, one answer:** every signature in the header is compared in constant time
  against each secret. A wrong signature, a stale timestamp and a replay all get the same 401
  (`"Invalid webhook"`); only missing or malformed headers get a 400.
- **Replays:** each accepted delivery (its id and timestamp, as a SHA-256 digest under
  `Cache.state_key`) is counted in the cache's backend until its timestamp can't be accepted
  anymore, so a captured request sent again is refused by every process. Only accepted
  deliveries are counted: a forged copy can't burn the id of the real one. A retry of the sender
  has a new timestamp and signature and goes through, as it must after a failed attempt: make the
  endpoint idempotent on `message.id` (a unique constraint) if a message must not be processed
  twice. The backend must support counters, as every built-in backend does.
- **Backend down:** a 503 with `Retry-After` (fail closed): senders retry later, while a replay
  would run its effects again. `fail_open=True` accepts correctly signed webhooks without the
  replay check instead.
- **Rotation:** `FastAPIWebhooks([new, old])` accepts a signature by either secret;
  `WebhookSigner([new, old])` signs every message with both, so receivers with either secret
  accept it.
- **Without a dependency:** `webhooks.verify(headers, body)` checks the signature and the
  timestamp (not replays), for queues and scripts.

## Outbound calls

`ResilientTransport` is an httpx transport for the calls your app makes to other services: a
deadline for each call and a circuit breaker for each origin. It needs httpx2 (or httpx):

```python
import httpx2 as httpx

from fastapi_alcazaba.outbound import CircuitOpenError, ResilientTransport


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    transport = ResilientTransport(timeout=5, failure_threshold=5, reset_timeout=30)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.example.com") as client:
        app.state.partner_api = client
        yield


@app.get("/quote")
async def quote(request: Request) -> Quote:
    try:
        response = await request.app.state.partner_api.get("/quote")
    except CircuitOpenError:  # the service keeps failing: don't wait for it
        raise HTTPException(503, "Quotes are unavailable, try again later")
    return Quote.model_validate(response.json())
```

- **Deadline:** `timeout` bounds the whole call, from sending the request to the last byte of the
  response. httpx's own timeouts are per phase, and its read timeout restarts with every byte, so
  a server that trickles a byte every few seconds would keep the request open forever. Past the
  deadline, `httpx.TimeoutException`. It applies to each request the transport sends: with
  `follow_redirects=True`, every hop gets a deadline of its own.
- **Response size:** responses are read whole before they're returned, so don't stream large
  downloads through it. `max_response_size` caps the bytes read (as received, before
  decompression); past it, `ResponseTooLargeError`, an `httpx.RequestError`.
- **Circuit breaker:** each origin (scheme, host and port) has its own. After
  `failure_threshold` consecutive failures, calls to that origin fail at once with
  `CircuitOpenError` for `reset_timeout` seconds, without reaching the server; then a single
  trial call decides whether it's back. `CircuitOpenError` is an `httpx.TransportError`, so code
  that already handles httpx errors handles it too. `transport.state(url)` says `"closed"`,
  `"open"` or `"half-open"`, handy in a health check. `failure_threshold=None` turns it off.
- **What counts as a failure:** the origin's transport errors (connection and protocol errors,
  timeouts, yours and httpx's) and responses whose status is in `failure_statuses` (500, 502, 503
  and 504 by default), which are still returned to you. Any other response, 4xx included, is a
  success. Your own errors are neither: an illegal header value (`httpx.LocalProtocolError`), a
  URL scheme httpx can't send (`httpx.UnsupportedProtocol`), an exception from your request body,
  `ResponseTooLargeError`. Otherwise anyone who can put a string in an outbound header could
  switch the integration off for everybody.
- **Your own transport settings:** `ResilientTransport(httpx.AsyncHTTPTransport(verify=...,
  limits=..., proxy=...))`.

### URLs your users give: SSRF

Calling a URL that a user typed (a webhook endpoint, a link preview, an avatar by URL) lets them
make your server call its own network: `http://169.254.169.254/` (the cloud's metadata service
and its credentials), `http://localhost:6379/`, an admin panel on `10.0.0.0/8`.
`SSRFGuardTransport` only calls the public internet:

```python
from fastapi_alcazaba.outbound import (
    BlockedDestinationError,
    ResilientTransport,
    SSRFGuardTransport,
)

client = httpx.AsyncClient(
    transport=ResilientTransport(SSRFGuardTransport(ports=(443,)), timeout=10),
    follow_redirects=True,
)

try:
    await client.post(subscription.url, content=body, headers=signer.sign(body))
except BlockedDestinationError:
    ...  # tell the user the URL isn't allowed
```

- **What's refused:** a host with any address that isn't public: private, loopback, link-local
  (169.254.0.0/16 and its metadata services, `fe80::/10`), carrier-grade NAT (100.64.0.0/10),
  multicast, reserved, documentation and benchmarking ranges, unique-local IPv6 (`fc00::/7`, AWS's
  `fd00:ec2::254`), and the IPv6 forms of those IPv4 addresses (`::ffff:127.0.0.1`, NAT64
  `64:ff9b::/96`, 6to4, Teredo). Literal addresses in URLs are checked without DNS; anything else
  goes to DNS, so numeric spellings (`http://2130706433/`, `http://0x7f.1/`) and `localhost`
  are refused for what they resolve to. Also refused: schemes other than http and https and, with
  `ports`, other ports. `BlockedDestinationError` says which host, never the address it resolved
  to (that's logged instead).
- **Pinned against DNS rebinding:** the transport resolves the host itself and connects to the
  address it checked: the URL is rewritten to it, the `Host` header keeps the name, and TLS gets
  the name for SNI and the certificate check. A DNS server that answers a public address to the
  check and a private one to the connection gets nowhere. If the first address can't be reached,
  one of the other family (IPv4/IPv6) is tried.
- **Redirects:** with `follow_redirects=True`, every hop goes through the transport and is checked
  like the first one.
- **Allowed internal destinations:** `allow=["billing.internal", "*.svc.cluster.local",
  "10.1.2.0/24"]` lets those names (exact, or subdomains) and networks through although they
  aren't public; they're still pinned.
- **With `ResilientTransport`:** wrap the guard in it (not the other way round): the deadline
  then covers the DNS lookup and each breaker is per host name. `BlockedDestinationError` is an
  `httpx.RequestError` but not a `TransportError`, so it doesn't count against the origin's
  breaker or trigger retry policies.
- **Connections:** each host name gets an `httpx.AsyncHTTPTransport` of its own (sharing one SSL
  context), at most `max_hosts` (100) kept open: pinned connections are pooled by address, and two
  names on one address (a CDN) must not share a connection whose certificate was checked for the
  other. Pass a factory for your own settings, `SSRFGuardTransport(lambda:
  httpx.AsyncHTTPTransport(http2=True))`, or a transport instance (a mock in tests), which then
  carries every request.
- **Proxies:** a proxy connects on its own, so pinning can't work through it:
  `httpx.AsyncHTTPTransport(proxy=...)` as the inner transport is refused. Don't pass `proxy=` or
  `mounts=` to the client either: those requests would bypass the transport. (The client ignores
  the `HTTP_PROXY` variables when it's given a transport.) An egress proxy that filters
  destinations itself, such as Smokescreen, is the alternative.

## Security headers and body size

Two ASGI middlewares for what FastAPI leaves to the deployment:

```python
from fastapi_alcazaba.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware

app.add_middleware(
    BodySizeLimitMiddleware,
    max_size=1_048_576,  # 1 MiB
    paths={"/uploads": 50 * 1_048_576, "/internal/stream": None},
)
app.add_middleware(SecurityHeadersMiddleware)  # last: the outermost
```

- **Security headers:** the OWASP REST ones: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cross-Origin-Opener-Policy:
  same-origin`. Headers the app sets itself are never overwritten; `headers=` adds others or
  changes the defaults (None removes one). Add it last, the outermost, so that the answers of
  the middlewares inside it (`BodySizeLimitMiddleware`'s 400 and 413) get them too. Starlette's
  500 for an unhandled exception never does: `ServerErrorMiddleware` wraps every middleware you
  add.
- **HSTS** only on HTTPS requests: browsers ignore it over plain HTTP, and on a development
  `localhost` it would stick. Behind a proxy that terminates TLS, let the server trust it
  (`uvicorn --proxy-headers --forwarded-allow-ips=<proxy address>`).
- **Content-Security-Policy:** `default-src 'none'; frame-ancestors 'none'` for everything that
  isn't HTML (nothing in a JSON response should load anything). HTML responses get `html_csp`,
  None by default, so that the Swagger UI and ReDoc, which load scripts from a CDN, keep working.
- **Body size:** FastAPI has no limit of its own; a client could send an endless body that the app
  reads into memory. A `Content-Length` over the limit gets a 413 before the app runs, a malformed
  or ambiguous one a 400. Bodies without one (chunked) are counted while the app reads them and
  refused once they pass the limit, also when FastAPI parses them into a model. `paths` sets
  other limits by path prefix (the longest wins, None for no limit), matched like the routes,
  without the `root_path`.
- Both are plain ASGI middlewares, not `BaseHTTPMiddleware`: streaming responses still stream,
  and WebSockets go through untouched.

## CSRF protection

A page on another site can make the user's browser send a form, or a `fetch`, to your app, and
the browser adds the user's cookies by itself. `CSRFMiddleware` refuses those requests with a 403,
from the headers browsers send to say where a request comes from:

```python
from fastapi_alcazaba.csrf import CSRFMiddleware

app.add_middleware(
    CSRFMiddleware,
    trusted_origins=["https://admin.example.com"],  # other origins whose pages may post here
    exempt_paths=["/webhooks"],  # signed in their own way
)
```

- **Fetch metadata first:** every current browser sends `Sec-Fetch-Site`. `same-origin` (and
  `none`, a request the user typed) goes through; `same-site` and `cross-site` are refused unless
  their `Origin` is in `trusted_origins`. Sibling subdomains are other origins too.
- **Older browsers:** without `Sec-Fetch-Site`, the `Origin` header (or else the `Referer`) must
  be the app's own origin or a trusted one. `Origin: null` (sandboxed frames, redirects) is
  refused, and so is a request with neither header.
- **Only what the browser adds by itself is protected:** requests without cookies or remembered
  HTTP authentication (Basic, Digest, Negotiate, NTLM) go through, so APIs that take a bearer
  token in a header keep working from anywhere (a page on another site can't set the header).
  Except form submissions from other sites (`Sec-Fetch-Mode` other than `cors`): before the user
  has a cookie they would log the browser into the attacker's account (login CSRF).
  `check_all=True` checks every request, for apps that trust TLS client certificates or the
  network.
- **Safe methods** (GET, HEAD, OPTIONS, TRACE) always go through: they mustn't change anything.
- **The app's own origin** is the scheme and the `Host` header as the server sees them. Behind a
  proxy that terminates TLS, let the server trust it (`uvicorn --proxy-headers
  --forwarded-allow-ips=<proxy address>`) and have the proxy pass `Host` on; otherwise every
  request looks cross-site, so list the public origin in `trusted_origins`.
- **Double-submit token** for single-page apps, as defence in depth: with `require_token=True`
  every response to a request without a valid token sets a random one (256 bits) in the
  `__Host-csrftoken` cookie (readable by JavaScript, `Secure`, `SameSite=Lax`, the `__Host-`
  prefix keeps sibling subdomains from planting one), also in `request.state.csrf_token`. Requests
  with cookies must send it back in `X-CSRF-Token`; it's compared in constant time. A browser that
  sends neither Origin nor Referer passes with a valid token. HTML forms without JavaScript can't
  set headers: rely on the origin checks for them.
- **WebSockets:** browsers don't apply CORS to WebSocket handshakes, so any site could open one
  with the user's cookies and talk to it (cross-site WebSocket hijacking). Handshakes with cookies
  get the same checks and are closed with 1008 before they're accepted (the server answers 403);
  handshakes without `Origin` aren't from a browser and go through.
- Refusals are logged at INFO with the method and the reason (never cookies or tokens), and
  counted in `fastapi_alcazaba.csrf.refused`. A plain ASGI middleware: streaming responses still
  stream.

## Server-side sessions

Starlette's `SessionMiddleware` keeps the whole session in a signed cookie: the client can read
it (it's only base64), it can't be revoked (a copied cookie keeps working after logout, until it
expires) and it can't hold much more than 4 KB. `ServerSessionMiddleware` keeps the data in the
cache's backend instead, and the cookie only carries a random id; `request.session` works the
same:

```python
from fastapi_alcazaba.sessions import ServerSessionMiddleware, clear_session, rotate_session

app.add_middleware(ServerSessionMiddleware, max_age=8 * 3600, idle_timeout=1800)


@app.post("/login")
async def login(request: Request, form: Annotated[Login, Form()]) -> RedirectResponse:
    user = await authenticate(form)
    request.session["user_id"] = user.id
    rotate_session(request)  # a new id: one planted in the browser before is now worthless
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    clear_session(request)  # deleted from the backend: a copied cookie is worth nothing now
    return RedirectResponse("/", status_code=303)
```

- **The cookie** holds 256 random bits (`secrets.token_urlsafe(32)`), `HttpOnly`, `Secure` and
  `SameSite=Lax` by default, with `Max-Age` = `max_age`. Use `cookie_name="__Host-session"` so that
  a sibling subdomain can't plant one (cookie tossing).
- **The backend** keeps the data under a SHA-256 digest of the id (whoever reads the keys can't use
  them as cookies), sealed with the cache's `signing_key` or `encryption_key` (use the latter when
  sessions hold personal data). Without either, whoever can write to the backend can forge a
  session, and a warning is logged. With a `TieredBackend` only the shared tier is used, so a
  logout is seen by every process at once.
- **Saved only when it changed**, nested changes included (`request.session["cart"].append(...)`),
  when the response starts. Empty sessions aren't stored and get no cookie; a session the app
  empties is deleted and its cookie expired. Responses that read it get `Vary: Cookie`, and those
  that set the cookie `Cache-Control: no-store` unless they set their own.
- **New ids are always the server's:** a cookie whose session doesn't exist (expired, made up)
  never becomes the id of a new session. `rotate_session` gives a new id with the same data (and
  a fresh `max_age`) at login, against session fixation; `clear_session` deletes the session and
  expires the cookie at logout, and data stored afterwards goes to a new session.
- **Expiry:** `max_age` seconds after creation or rotation, and `idle_timeout` seconds without
  requests (refreshed in the backend at most every tenth of it, not on every request). Both are
  checked when the session is read, and the backend drops it then as well.
- **JSON only**, up to `max_size` bytes (16 KiB): more, or a value that isn't JSON, raises when
  the response starts.
- **Backend failures:** if the session can't be read, the request goes on with an empty one
  (anonymous, which fails closed) and a warning. Changing it then, or failing to save, rotate or
  clear it, replaces the response with a 503 and leaves the cookie alone: the response would claim
  a login or a logout that didn't happen. Concurrent requests of one session: the last save wins.
- WebSockets get `websocket.session` too, read-only (`rotate_session` and `clear_session` raise
  there). A plain ASGI middleware: streaming responses still stream.

## Checking uploads

The name and the `Content-Type` of an upload are whatever the client chose: an HTML page sent as
`cat.png` would be served back as HTML. `FastAPIUploads` checks the file itself, with nothing but
the standard library:

```python
from fastapi_alcazaba.uploads import IMAGES, CheckedFile, FastAPIUploads

uploads = FastAPIUploads(types=IMAGES, max_size=5 * 1024 * 1024, max_pixels=25_000_000)


@app.post("/avatars")
async def avatar(upload: Annotated[CheckedFile, Depends(uploads)]) -> dict[str, str]:
    name = f"{uuid4()}{upload.extension}"  # never the client's name
    await storage.save(name, upload.file.file, content_type=upload.content_type)
    return {"name": name}
```

As a dependency it reads the form field `file`; for other fields or several files, call
`await uploads.check(file)`. It returns a `CheckedFile`: the `UploadFile` read back to its start,
the `content_type` found in its bytes, its `size`, its `width` and `height` (images) and a safe
`filename`.

- **The real type** (`sniff(data)`): PNG, JPEG, GIF, WebP, PDF, ZIP, GZIP, MP4 and QuickTime from
  their signatures; Word, Excel and PowerPoint documents from the content types their
  `[Content_Types].xml` declares (as Office reads them), with the macro-enabled ones (`.docm`,
  `.xlsm`, `.pptm`) told apart, and anything ambiguous reported as a plain ZIP; UTF-8 text without
  control characters (`text/plain`: CSV is text, it can't be told apart). Text that starts with a
  tag is HTML, SVG or XML, never plain text, and SVG is never an image: all three can carry
  scripts. `types` must come from `KNOWN_TYPES` (`IMAGES`, `OFFICE_DOCUMENTS`...), so a typo like
  `image/jpg` fails at start-up. Store and serve files with the detected type, never the
  client's, plus `X-Content-Type-Options: nosniff` (`SecurityHeadersMiddleware`).
- **Size:** counted while the file is read in chunks, whatever `UploadFile.size` says; 413 beyond
  `max_size`, 415 for a type that isn't allowed, 422 for an empty file. By the time an endpoint
  runs the multipart parser has stored the whole request already, so limit it with
  `BodySizeLimitMiddleware` on the route as well.
- **Decompression bombs:** the width and height of PNG, JPEG (however much metadata comes before
  the frame), GIF and WebP images are read from their headers (`image_size(data)`) without
  decoding them; more than `max_pixels` (40 million by default) gets a 413, and an image whose
  dimensions can't be read a 415.
- **Safe names** (`safe_filename(name)`): NFKC-normalised, only the last path component, without
  control or invisible characters (such as the right-to-left override that shows `exe.txt` as
  `txt.exe`), `<>:"|?*` replaced, without leading dots or dashes, `_` in front of the names Windows
  reserves (`CON`, `NUL`, `COM1`...), and at most 255 bytes, keeping the extension. For display and
  `Content-Disposition`; store files under names of your own (`CheckedFile.extension`).
- **What it doesn't do:** look inside the files. A PDF can carry scripts, Office documents
  macros, archives may expand to far more than their size, and the frames of a GIF aren't counted.
  Allow HTML, SVG or XML only to serve them as attachments from another domain.

## Problem details

`FastAPIProblemDetails` answers errors with RFC 9457 problem details
(`application/problem+json`) instead of FastAPI's `{"detail": ...}`, and without telling clients
what they shouldn't know:

```python
from fastapi_alcazaba.problems import FastAPIProblemDetails, Problem

problems = FastAPIProblemDetails()
problems.install(app)


@app.post("/orders")
async def order(item: Item) -> Order:
    if stock[item.id] < item.quantity:
        raise Problem(
            409,
            f"Only {stock[item.id]} left",
            type="https://example.com/problems/out-of-stock",
            title="Out of stock",
            extensions={"available": stock[item.id]},
        )
    ...
```

```json
{"type": "https://example.com/problems/out-of-stock", "title": "Out of stock", "status": 409,
 "detail": "Only 3 left", "instance": "/orders", "available": 3, "request_id": "4bf92f35…"}
```

- **Members:** `type` (`about:blank` unless a `Problem` says otherwise), `title` (the RFC 9110
  status phrase), `status`, `detail` when there's something to tell, `instance` (the request's
  path, percent-encoded) and `request_id` when `RequestIDMiddleware` gave the request one.
- **HTTPExceptions:** FastAPI's, Starlette's and their subclasses (`BreakerOpen`, the rate
  limiter's 429…) keep their headers: `Retry-After`, `WWW-Authenticate`, `Allow`. A 4xx's detail
  is sent, since it's written for clients (`expose_detail=False` keeps it); a 5xx's never is: an
  `HTTPException(500, str(error))` becomes a bare 500, and the hidden detail is logged as a
  warning. A detail that isn't a string goes in an `errors` member. Codes under 400 (a redirect
  raised as an exception) keep FastAPI's answer.
- **Validation errors:** a 422 whose `errors` list each error's `loc`, `msg` and `type`, never
  its `input` (FastAPI echoes what was sent, passwords included) nor its `ctx`. At most
  `max_errors=100`, so that a huge invalid body doesn't get a huge answer.
- **Unhandled exceptions:** a 500 that says nothing about them. Starlette raises the exception
  again after the response, so the server logs it with its traceback (uvicorn's "Exception in
  ASGI application", with the request ID when you use `RequestIDLogFilter`) and `TestClient`
  raises it in tests; it isn't logged a second time. With `debug=True` Starlette shows its
  traceback page instead.
- **Problem:** an `HTTPException` with a `type` URI, a `title` and extension members, whose names
  are checked (three or more letters, digits or `_`, starting with a letter; never a standard
  member) and whose values are encoded when it's raised. Its detail is always sent, also with a
  5xx: you wrote it for the client. `problems.response(request, Problem(...))` answers one from
  your own exception handlers.
- Handlers you register for a status code or an exception subclass still win; install it before
  the app starts.

## Request IDs

`RequestIDMiddleware` gives every request an ID, to find all the log lines of one request and to
let a client quote it when it reports an error:

```python
import logging

from fastapi_alcazaba.request_id import RequestIDLogFilter, RequestIDMiddleware, request_id

app.add_middleware(RequestIDMiddleware)  # last: the outermost

handler = logging.StreamHandler()
handler.addFilter(RequestIDLogFilter())
handler.setFormatter(logging.Formatter("%(asctime)s [%(request_id)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(handler)


@app.post("/jobs")
async def create_job() -> dict[str, str | None]:
    return {"ticket": request_id()}
```

With uvicorn, add the filter to its handlers too (`"filters": {"request_id": {"()":
"fastapi_alcazaba.request_id.RequestIDLogFilter"}}` in its `log_config`), and its access and
error lines carry the ID.

- **Incoming IDs:** the request's `X-Request-ID` (`header=` for another) is reused when a proxy
  or the client set one that's safe to log: a single value matching `[A-Za-z0-9._:-]{1,128}`.
  Anything else (line breaks that would forge log lines, an endless value, several values) gets
  a new random one, 128 bits in hex. `trust_incoming=False` always makes a new one.
- **Everywhere in the request:** `request_id()` returns it in endpoints, dependencies, sync
  endpoints' threads and WebSockets; None outside a request. The response carries it in the
  same header (unless the app set that header itself), and so do WebSocket handshakes.
- **In errors:** after an unhandled exception the ID stays set for the rest of the request's
  task, so Starlette's 500 handler and the server's error log, which run outside every
  middleware, still know which request failed. `FastAPIProblemDetails` puts it in every problem,
  500s included, and in their header.
- **In logs:** `RequestIDLogFilter` sets `record.request_id` (`default="-"` outside a request; a
  `request_id` passed in `extra=` is kept). Add it to handlers: a logger's filters don't see the
  records of its child loggers.
- Add it last, the outermost, so that the answers of the other middlewares (a 413, a 504) carry
  the ID too.

## Request timeouts

`RequestTimeoutMiddleware` gives every request a deadline, so that a hung dependency or a client
that trickles its body doesn't hold a request (and its database connection, its memory…)
forever:

```python
from fastapi_alcazaba.timeouts import RequestTimeoutMiddleware

app.add_middleware(
    RequestTimeoutMiddleware,
    timeout=30,
    paths={"/reports": 120, "/events": None},  # Server-Sent Events: no deadline
)
```

- **Before the response starts:** at the deadline the app is cancelled, its `finally` blocks and
  the exit of its `yield` dependencies run, and the client gets a 504
  (`{"detail": "Request timed out"}`). Reading the request body counts: a client that sends it a
  byte at a time runs out of time too.
- **While a response streams:** it's too late for a 504. The app is cancelled all the same and
  the TimeoutError raised again, so the server closes the connection: the client sees an
  incomplete body (an unfinished chunked read), never a short one that looks complete. Give
  streaming endpoints their own deadline in `paths`, or None.
- **After the response:** once its last byte is sent the deadline is off, so background tasks run
  as long as they need.
- **Sync endpoints** run in a thread that can't be interrupted: the client gets its 504 on time,
  but the thread runs to its end, after the exit of its `yield` dependencies has run. Endpoints
  that may hit the deadline are better async.
- **Paths:** `paths` sets other deadlines by path prefix (the longest wins, None for none),
  matched like the routes, without the `root_path`; `timeout=None` leaves the other paths
  without one. The app's own TimeoutErrors go through untouched, and WebSockets and lifespan
  events too.
- Both cases are logged as warnings with the method and the route template. A plain ASGI
  middleware: streaming responses still stream.

## Health checks

`health_router()` adds the two endpoints that load balancers and orchestrators probe:

```python
from fastapi_alcazaba.health import HealthCheck, health_router


async def database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


app.include_router(
    health_router({"database": database, "search": HealthCheck(ping_search, critical=False)})
)
```

Point the liveness probe at `/health/live` and the readiness probe (or the load balancer's
health check, e.g. the target group of an ECS service) at `/health/ready`.

- **Live vs ready:** `/health/live` only says that the process answers. `/health/ready` runs the
  checks concurrently, each with a timeout (`timeout=2.0`, or the check's own), and answers 200
  (`ok`, or `degraded` when only non-critical checks fail) or 503 (`fail`) with each check's
  status and duration. Keep dependencies out of liveness: a probe that checks the database gets
  healthy containers restarted every time it blips. A check passes when it returns anything but
  `False` without raising. Sync checks run in a daemon thread of their own, off the event loop,
  so one that hangs holds up neither the shutdown nor the threads of the loop's default executor;
  it isn't started again, under the same name in the same router, until its thread finishes.
- **The cache is checked, but isn't critical:** the cache's backend (`cache=`, else the app's or
  `FastAPICache`'s) is asked for a key under its prefix, straight, not through the circuit
  breaker. The cache fails open, so its outage makes the app `degraded`, not unready: taking
  every instance out of the load balancer would turn a Redis outage into a full one.
  `include_cache=False` leaves it out.
- **No leaked errors:** failures are logged with their traceback and never returned: a public
  probe mustn't tell the world the database's hostname or connection string.
- **Probes don't pile up:** concurrent `/ready` requests share one run of the checks, and its
  result answers the next ones for `reuse_for=1.0` seconds, so a flood of probes doesn't flood
  the database. A probe that goes away doesn't cancel the run the others are waiting for.
- The endpoints are left out of the OpenAPI schema unless `include_in_schema=True`; responses
  carry `Cache-Control: no-store`.

## Scheduled jobs

`FastAPIScheduler` runs jobs every N seconds or by a cron expression, in every process of the
app, and each tick once across them all:

```python
from fastapi_alcazaba import FastAPIScheduler

scheduler = FastAPIScheduler()  # FastAPICache's backend; or FastAPIScheduler(cache)


@scheduler.every(minutes=5)
async def refresh_catalog() -> None: ...


@scheduler.cron("0 3 * * *", tz="Europe/Madrid")
def nightly_report() -> None: ...  # sync jobs run in a thread


@scheduler.every(seconds=30, exclusive=False)
async def reload_settings() -> None: ...  # in every process: what each keeps in memory


app = FastAPI(lifespan=scheduler.lifespan)  # or `async with scheduler:` in your lifespan
```

- **Once per tick:** at each tick, the processes claim it in the backend, with an atomic counter
  named after the job and the tick, and only the first runs it. A lock held while the job runs
  keeps a run that outlasts its interval from overlapping the next tick in another process
  (memcached can't hold locks: there, each tick still runs once, but long runs may overlap).
  The ticks are the same in every process: `every(minutes=5)` runs at :00, :05… (UTC), so keep
  the servers' clocks in sync (NTP). `exclusive=False` runs a job in every process.
- **Cron:** five fields (minute, hour, day of the month, month, day of the week, Sunday being 0 or
  7) with `*`, lists, ranges, steps and names (`*/15 9-17 * * mon-fri`), or `@hourly`, `@daily`,
  `@weekly`, `@monthly` and `@yearly`, in the time zone `tz` (UTC by default). When the clocks
  change it does what cronie does: a job at a fixed time runs an hour later on the day its hour
  is skipped and once on the day it repeats; a job with `*` in its minute or hour follows real
  time. No dependency: the parser is Alcazaba's.
- **No drift, no bursts:** the next run is the next tick after the clock, not a delay after the
  last run ended; ticks missed while a run took longer than its interval are skipped, not
  run in a row afterwards.
- **Failures:** an exception is logged with its traceback, counted, and the job keeps its
  schedule. If the backend fails, the tick is skipped (`fail_open=True` runs it anyway, maybe in
  every process).
- **Start and stop:** the jobs start with the app and never hold up its startup. At shutdown,
  jobs waiting for their next run stop at once and running ones get `grace` seconds (10) before
  they're cancelled; a sync job's thread runs to its end. `scheduler.next_run(name)` says when a
  job runs next. A job is named after its module and function (`name=` for another), the name
  that its ticks have in the backend.

## Task queue

`FastAPITaskQueue` hands work over from an endpoint to run in the background, in this process or
in any other that shares the cache's Redis or Valkey:

```python
from fastapi_alcazaba import FastAPITaskQueue

queue = FastAPITaskQueue()  # FastAPICache's backend; or FastAPITaskQueue(cache)


@queue.task(retries=5)
async def send_welcome_email(user_id: int) -> None: ...


app = FastAPI(lifespan=queue.lifespan)  # this process runs tasks too


@app.post("/users")
async def create_user(user: NewUser) -> None:
    ...
    await queue.enqueue(send_welcome_email, user.id)  # delay=60 or a timedelta to wait first
```

- **At least once:** tasks live in a Redis stream read by a consumer group, and leave it only once
  they've finished. A process that dies while running one, or can't reach the backend to say it
  finished, means that it runs again elsewhere: write tasks so that running one twice does no harm
  (check first whether the e-mail went out). A worker that stops answering has its tasks taken
  over after `visibility_timeout` (60 s); running tasks are kept from that by heartbeats, however
  long they run. A task handed out more than `max_deliveries` times (5) goes to the dead letters
  instead: it may be what kills the workers.
- **Only registered tasks run, with JSON arguments**, never pickle. `enqueue` checks that the
  arguments fit the function and serialise (TypeError otherwise), up to `max_payload_size` bytes
  (64 KiB); they arrive as JSON gives them back (tuples as lists).
- **Sealed:** with a `signing_key` or `encryption_key` on the cache, every task is signed or
  encrypted and bound to its queue. A task that someone planted in the backend or altered is
  refused, logged and sent to the dead letters, never run. Without a key a warning is logged at
  start: whoever can write to the backend can run your tasks with arguments of their choosing.
- **Retries:** a task that raises is tried again `retries` times (3), `backoff` seconds after the
  first failure (1), twice that after the second… up to `max_backoff` (600), each with jitter so
  that tasks that failed together (a service down) don't all retry at once. Then it goes to the
  dead letters with the type of its error, never the message, which may hold personal data.
  `dead_letters()` lists them, `requeue_dead_letters()` puts them back with their attempts reset,
  and `discard_dead_letters(ids)` deletes them.
- **Delayed tasks and retries** wait in a sorted set, and move to the stream when they're due in
  a Lua script: two processes never both move one.
- **Bounded:** the queue holds at most `max_length` tasks (100,000: waiting, running or delayed),
  and `enqueue` raises `QueueFull` beyond, instead of dropping any; about `max_dead_letters`
  (10,000) are kept. `pending()` counts the tasks that haven't finished.
- **Workers:** `start()` and `stop()`, `lifespan` or `async with`, running `concurrency` tasks at a
  time (10) in each process; sync functions run in a thread. `stop()` takes no more tasks and gives
  the running ones `grace` seconds (10); those still running then are cancelled and go back to the
  queue at once (a sync task's thread can't be stopped: it runs to its end, so that task may run
  twice). A process that only runs tasks: `async with queue: await asyncio.Event().wait()`.
- **Backends:** Redis 6.2+ or Valkey through `RedisBackend` or `ValkeyBackend` (or a
  `TieredBackend` in front of one), clusters included: a queue's keys share a hash tag. Other
  backends raise `NotImplementedError`. Its keys are state keys, which `clear()` never deletes.
- **Small on purpose:** no results, priorities or workflows. Each run is the root of a trace of its
  own, linked to the span of the request that enqueued it.

## Circuit breakers

`FastAPIBreaker` stops calling a dependency that keeps failing (a database, an SDK, a service
reached without httpx), so that requests fail at once instead of waiting for it, and it gets room
to recover:

```python
from fastapi_alcazaba import FastAPIBreaker

payments = FastAPIBreaker("payments", failure_threshold=5, reset_timeout=30, timeout=10)


@payments
async def charge(order: Order) -> Receipt:
    return await payments_sdk.charge(order.id, order.total)


async def refund(order: Order) -> None:
    async with payments.attempt():  # or around a block
        await payments_sdk.refund(order.id)
```

- **States:** after `failure_threshold` consecutive failures it opens, and for `reset_timeout`
  seconds calls fail with `BreakerOpen` without reaching the dependency. Then a single trial call
  goes through: its success closes the breaker, its failure opens it again.
- **In FastAPI:** `BreakerOpen` is an `HTTPException`: an endpoint that hits an open breaker
  answers 503 with `Retry-After` (the seconds until the trial), without naming the dependency.
- **What counts:** exceptions in `failures` (any `Exception` by default) are failures; those in
  `exclude` mean the dependency answered (a "not found" from its SDK) and are successes; others,
  and a cancelled call, give no verdict. `timeout` bounds async calls: a slower call is
  cancelled, raises `TimeoutError` and counts as a failure. Sync code can't be interrupted, so a
  breaker with a timeout refuses to guard it.
- **Per process:** each process keeps its own state, with no round trip on every call. Sync
  functions work too, also from the threads of FastAPI's pool.
- **Observability:** opening is logged as a warning and closing again as info, and both are
  counted with the calls (`fastapi_alcazaba.breaker.*` metrics). `breaker.state` says
  `"closed"`, `"open"` or `"half-open"`, handy in a health check:
  `HealthCheck(lambda: payments.state != "open", critical=False)`.

## Concurrency limits

`FastAPIConcurrencyLimiter` limits how many requests run *at the same time*, across every process
and container, for endpoints whose cost is how long they run (AI, OCR, PDF rendering). The rate
limiter counts requests per window; this counts the ones in flight:

```python
from fastapi_alcazaba.concurrency import FastAPIConcurrencyLimiter
from fastapi_alcazaba.limiter import client_ip

ocr = FastAPIConcurrencyLimiter(4, name="ocr", wait=2)  # FastAPICache's backend, or cache=...


@app.post("/invoices/scan", dependencies=[Depends(ocr)])
async def scan(file: UploadFile) -> Invoice: ...


@app.post("/summaries", dependencies=[Depends(FastAPIConcurrencyLimiter(1, identifier=client_ip))])
async def summarise(text: str) -> Summary: ...  # one at a time per client


async def scan_pending() -> None:  # outside requests: a job, a queue consumer
    async with ocr.slot():
        ...
```

- **Slots in the backend:** `limit` locks (up to 100), tried in random order so that the
  processes don't all queue on the first; a request holds one while it runs, renewed every
  `ttl / 3` seconds, and the slots of a process that dies expire after `ttl` (60). Keys are
  SHA-256 digests: client identities never reach the backend in clear.
- **When it's full:** a request waits up to `wait` seconds (0: tries once), polling with
  jittered backoff, then gets a 503 with `Retry-After` (`retry_after`, 1 s by default); a 429 when
  the limit is per client (`identifier`). There's no queue and no order among the waiters.
  WebSockets are closed with 1013 (1008 per client) and hold their slot while connected.
- **Sharing:** by default each route (methods and path template) has its own slots; limiters with
  the same `name` and `limit` share them. `slot(key=...)` gives each key its own slots outside
  requests (it needs a `name`), and `slot(connection=request)` takes one around just the
  expensive part of an endpoint.
- **Streaming:** the slot is released when FastAPI runs the dependency's exit code. Since FastAPI
  0.118 that's after the response is sent, so a `StreamingResponse` keeps its slot until its last
  byte; `Depends(limiter, scope="function")` (FastAPI 0.121) releases it when the endpoint
  returns, before the body. FastAPI 0.115 to 0.117 release it before the response is sent: take
  a `slot()` inside the stream there.
- **Failures:** if the backend fails, each process counts the slots on its own (`fail_open=True`,
  the default: at most `limit` per process run); `fail_open=False` refuses with a 503 instead
  (`slot()` raises `BackendUnavailable`). memcached can't hold locks.
- **Limits:** like any lock on a cache-like store, a holder that stalls for longer than `ttl`
  loses its slot while it still runs, and someone else may take it. Taking a slot when most are
  busy costs a round trip per busy slot tried.

## Leader election

`FastAPILeader` elects one process among all those sharing the backend, for work that must run in
exactly one of them all the time (a queue consumer, an outbox relay, a watcher) rather than at
given times (that's the scheduler):

```python
from fastapi_alcazaba.leader import FastAPILeader

leader = FastAPILeader("outbox")  # FastAPICache's backend; or FastAPILeader("outbox", cache=cache)


@leader.run
async def relay_outbox() -> None:
    while True:
        await relay_pending()
        await asyncio.sleep(1)


app = FastAPI(lifespan=leader.lifespan)  # or `async with leader:` in your lifespan

...
if leader.is_leader:  # anywhere, sync code included
    ...
```

- **Election:** every process campaigns for a lock named after the leader, renewed every
  `ttl / 3` seconds (`ttl` is 15 by default). The one that takes it leads and runs the functions
  given to `run`; the others try again every `ttl / 3` seconds or so, with jitter.
- **Hand-over:** a leader that stops releases the lock, and another process takes over within
  `ttl / 3` seconds; one that dies, within `ttl` and a third. A leader that loses the lock (its
  renewals couldn't keep up) cancels its functions within a second and campaigns again; at the
  next election they run again.
- **Functions:** async, without arguments, added before `start()`. They're cancelled when the
  leadership ends and must let the cancellation through. An exception is logged with its
  traceback; the process keeps leading.
- **Failures:** while the backend is down nobody is elected, and a leader that can't renew steps
  down when its lock expires: no leader is better than two. memcached can't hold locks.
- **Limits:** a leader that stalls for longer than `ttl` (a long pause, a network partition) can
  overlap the next one for a moment. Check `is_leader` between the steps of long work, and keep
  that work idempotent.

## Maintenance mode

`FastAPIMaintenance` is a switch shared by every process; while it's on, `MaintenanceMiddleware`
answers 503 with `Retry-After` to every request but the allowed ones:

```python
from fastapi_alcazaba.limiter import client_ip
from fastapi_alcazaba.maintenance import FastAPIMaintenance, MaintenanceMiddleware

maintenance = FastAPIMaintenance()  # FastAPICache's backend; or FastAPIMaintenance(cache)
app.add_middleware(
    MaintenanceMiddleware,
    maintenance=maintenance,
    allow_paths=("/health", "/admin"),  # always served
    allow=lambda request: client_ip(request) in OFFICE,  # the team keeps working
)

await maintenance.enable("Back at 10:30", retry_after=900)  # from an admin route, a script...
await (
    maintenance.status()
)  # MaintenanceStatus(enabled=True, message=..., retry_after=900, since=...)
await maintenance.disable()
```

- **Shared, but not asked on every request:** the state lives in the backend, and each process
  keeps a copy refreshed at most every `refresh` seconds (1 by default), so a change reaches every
  process within about that. `status()` reads the backend now.
- **Responses:** a 503 whose `detail` is the message ("Service Unavailable" without one), with
  `Retry-After` when given and `Cache-Control: no-store`. WebSockets are closed with 1013 (try
  again later). Lifespan events go through. A pure ASGI middleware: streaming isn't affected.
- **Exceptions:** `allow_paths` are prefixes (`/health` covers `/health/live`, not `/healthz`),
  matched without the `root_path` of a proxy or a mount, and are served without reading the state.
  `allow` (sync or async) receives the request: use the address the server reports
  (`client_ip`), never `X-Forwarded-For`.
- **Failures:** when the backend fails, each process keeps the last state it read (maintenance
  often means work on the backend itself: its failure mustn't reopen an app closed on purpose)
  and serves normally if it never read one. `enable()` and `disable()` raise
  `BackendUnavailable`: nothing changes then.
- **Integrity:** with a `signing_key` or `encryption_key` on the cache the state is sealed, and a
  state planted in the backend by someone else is ignored; the message is capped at 1000
  characters.

## Feature flags

`FastAPIFeatureFlags` keeps flags in the backend: turn features on and off for everybody, for some
users or tenants, or for a percentage of them, from any process and without a deploy:

```python
from fastapi_alcazaba.flags import FastAPIFeatureFlags

flags = FastAPIFeatureFlags()  # FastAPICache's backend; or FastAPIFeatureFlags(cache, refresh=5)

await flags.set("new-checkout", enabled=True, percentage=10, allow=["user-42"], deny=["user-7"])

if await flags.enabled("new-checkout", subject=str(user.id)):
    ...


def user_id(request: Request) -> str | None:
    return request.headers.get("x-user-id")


@app.get("/beta/report", dependencies=[Depends(flags.require("beta-report", subject=user_id))])
async def beta_report() -> Report: ...  # 404 while the flag is off for the user
```

- **Rules:** a flag is on for a subject when it's enabled, the subject isn't in `deny`, and it's
  in `allow` or falls within `percentage` (none: everybody). `enabled=False` turns it off for
  everyone, `allow` included. Without a subject, a partial rollout counts as off. An undefined
  flag gives `default` (False).
- **Sticky rollouts:** a subject's place comes from a hash of the flag's name and the subject, so
  it keeps its answer while the percentage stays, raising the percentage only adds subjects, and
  each flag picks different subjects.
- **Hidden when off:** `require()` answers FastAPI's own `404 Not Found`, so the flag isn't
  revealed, and closes WebSockets with 1008. For subjects that need other dependencies (the
  current user), call `enabled()` from your own dependency.
- **Local copies:** each process keeps the definitions it reads for `refresh` seconds (5), so a
  change reaches every process within that, and a flag costs at most one round trip per
  `refresh`. When the backend fails, the last definition read is used, or the default.
  `get(name)` reads the backend now; `delete(name)` removes a flag.
- **Privacy and integrity:** subjects are stored as SHA-256 digests salted with the flag's name,
  never in clear (at most 10,000 per list). Whoever reads the backend can still confirm a guess
  of a guessable subject (an e-mail address): prefer opaque ids, or give the cache an
  `encryption_key`, which encrypts the definitions. With a `signing_key` or `encryption_key`,
  definitions planted by someone else are ignored. Names are up to 100 letters, digits, `.`, `_`
  and `-`.

## Resilience

The cache never makes a request fail. Every backend call has a timeout (`backend_timeout`, 1 s by
default): if it runs out, or the backend fails, the request goes on without cache. After
`circuit_breaker_failures` consecutive failures the backend isn't called at all for
`circuit_breaker_reset` seconds, so that requests don't each wait for the timeout; then one call
tries again.

Concurrent misses for the same key compute once per process (`single_flight`). With several
processes or containers, `distributed_lock=True` makes the first one take a lock in Redis/Valkey
while the others wait for its value (up to `lock_wait` seconds). And `early_refresh=0.1` lets
callers refresh an entry, with growing probability, during the last 10 % of its lifetime: popular
entries are renewed before they expire, instead of every caller missing at once. If that refresh
fails, the cached value is served.

## Metrics and traces

Alcazaba only uses the OpenTelemetry API (the `otel` extra): it records nothing until the app
configures an SDK, and never configures one itself. The simplest way is OpenTelemetry's own
zero-code instrumentation, with the standard variables:

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install  # instrumentations for FastAPI, httpx2, redis, SQLAlchemy…

OTEL_SERVICE_NAME=shop-api \
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production \
OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.internal:4317 \
OTEL_PYTHON_LOG_CORRELATION=true \
OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="health/live,health/ready" \
opentelemetry-instrument uvicorn app:app
```

Give the collector's endpoint explicitly, over TLS: traces and logs carry what your app does.
Then Alcazaba adds to them:

- **Spans:** one per cached call (`fastapi_alcazaba <function>`, with `fastapi_alcazaba.result`:
  hit, miss, refresh or bypass) and, inside it, one per backend call (`fastapi_alcazaba.backend
  get`, `set`, `lock`…, with failures recorded); instrumented Redis/Valkey clients nest their own
  inside. Each run of a scheduled job is the root of a trace of its own
  (`fastapi_alcazaba.scheduler <job>`, failures recorded), so what the job does (queries, calls)
  is grouped under it instead of scattered. So is each run of a task (`fastapi_alcazaba.task
  <task>`, with its queue, ID and attempt), linked to the span that enqueued it.
- **On the request's span:** the events `fastapi_alcazaba.rate_limit` (a request limited or
  unchecked), `fastapi_alcazaba.breaker.refused`, `.opened` and `.closed`,
  `fastapi_alcazaba.outbound.refused` (an outbound call to an origin whose breaker is open) and
  `fastapi_alcazaba.csrf.refused`, and the attribute `fastapi_alcazaba.idempotency` with the
  outcome of an Idempotency-Key.
- **Never:** keys, tokens, identities, codes or request bodies. Attributes are names, routes and
  outcomes, few enough to keep the cardinality of the metrics low.

| Metric | Attributes |
| --- | --- |
| `fastapi_alcazaba.lookups` | `fastapi_alcazaba.result` (hit, miss, refresh, bypass), `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.stores` | `fastapi_alcazaba.outcome` (stored, uncacheable, too_large, unencodable, failed), `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.entry.size` | `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.backend.duration` | `fastapi_alcazaba.operation` (get, set, delete, incr, lock, unlock, extend_lock, tag, incr_window) |
| `fastapi_alcazaba.backend.errors` | `fastapi_alcazaba.operation`, `error.type` |
| `fastapi_alcazaba.rate_limit` | `fastapi_alcazaba.outcome` (allowed, limited, unchecked), `fastapi_alcazaba.limiter` (its name, or the route) |
| `fastapi_alcazaba.breaker.calls` | `fastapi_alcazaba.outcome` (success, failure, refused), `fastapi_alcazaba.breaker` |
| `fastapi_alcazaba.breaker.transitions` | `fastapi_alcazaba.state` (open, closed), `fastapi_alcazaba.breaker` |
| `fastapi_alcazaba.scheduler.runs` | `fastapi_alcazaba.outcome` (ran, failed, skipped, overlapping, unchecked), `fastapi_alcazaba.job` |
| `fastapi_alcazaba.idempotency` | `fastapi_alcazaba.outcome` (stored, replayed, conflict, mismatch, not_kept, invalid, too_large, unchecked) |
| `fastapi_alcazaba.tokens.checks` | `fastapi_alcazaba.outcome` (valid, revoked, unchecked) |
| `fastapi_alcazaba.otp.verifications` | `fastapi_alcazaba.outcome` (success, wrong, missing, exhausted, malformed, unchecked): a burst of wrong or exhausted is someone guessing |
| `fastapi_alcazaba.lock.wait` | `fastapi_alcazaba.acquired` (true, false): seconds spent acquiring locks |
| `fastapi_alcazaba.lock.lost` | locks whose renewals couldn't keep up |
| `fastapi_alcazaba.health.checks` | `fastapi_alcazaba.check`, `fastapi_alcazaba.status` (ok, fail) |
| `fastapi_alcazaba.health.check.duration` | `fastapi_alcazaba.check` |
| `fastapi_alcazaba.concurrency` | `fastapi_alcazaba.outcome` (acquired, local: counted in the process while the backend failed, refused, unavailable), `fastapi_alcazaba.limiter` |
| `fastapi_alcazaba.leader.transitions` | `fastapi_alcazaba.leader`, `fastapi_alcazaba.event` (elected, lost, resigned) |
| `fastapi_alcazaba.maintenance.refused` | requests refused during maintenance |
| `fastapi_alcazaba.flags.evaluations` | `fastapi_alcazaba.flag`, `fastapi_alcazaba.result` (on, off, default) |
| `fastapi_alcazaba.problems` | `fastapi_alcazaba.status`, `fastapi_alcazaba.kind` (http_exception, problem, validation, unhandled) |
| `fastapi_alcazaba.request_timeouts` | `fastapi_alcazaba.outcome` (504, cut: already streaming), `fastapi_alcazaba.route` |
| `fastapi_alcazaba.request_ids` | `fastapi_alcazaba.source` (incoming, generated, rejected): many rejected may be a misconfigured proxy, or someone forging log lines |
| `fastapi_alcazaba.outbound.breaker` | `fastapi_alcazaba.event` (opened, refused), `server.address` |
| `fastapi_alcazaba.outbound.ssrf.blocked` | `fastapi_alcazaba.reason` (scheme, host, port, address) |
| `fastapi_alcazaba.webhooks` | `fastapi_alcazaba.outcome` (accepted, replayed, rejected, malformed, too_large, unchecked, unavailable) |
| `fastapi_alcazaba.api_keys` | `fastapi_alcazaba.outcome` (accepted, malformed, unknown, wrong_secret, expired, insufficient_scope) |
| `fastapi_alcazaba.csrf.refused` | `fastapi_alcazaba.kind` (http, websocket), `fastapi_alcazaba.reason` (fetch_site, origin, no_source, token) |
| `fastapi_alcazaba.sessions` | `fastapi_alcazaba.event` (created, updated, rotated, deleted, touched, rejected: forged or altered, unreadable, unavailable) |
| `fastapi_alcazaba.uploads` | `fastapi_alcazaba.outcome` (accepted, too_large, too_many_pixels, unsupported, unreadable, empty) |
| `fastapi_alcazaba.uploads.size` | `fastapi_alcazaba.content_type` (the detected one): bytes of the uploads accepted |
| `fastapi_alcazaba.tasks.enqueued` | `fastapi_alcazaba.queue`, `fastapi_alcazaba.task` |
| `fastapi_alcazaba.tasks` | `fastapi_alcazaba.queue`, `fastapi_alcazaba.task`, `fastapi_alcazaba.outcome` (succeeded, retried, failed, interrupted; for refused tasks malformed, tampered, too_large, unknown_task, invalid_arguments, abandoned, with the task only when it's registered) |
| `fastapi_alcazaba.tasks.duration` | `fastapi_alcazaba.queue`, `fastapi_alcazaba.task`, `fastapi_alcazaba.outcome` |
| `fastapi_alcazaba.tasks.reclaimed` | `fastapi_alcazaba.queue`: tasks taken over from workers that stopped answering |

## Responses in several languages

If an endpoint translates its response according to a request header, such as
`Accept-Language`, add that header to `vary_headers` (`@cache(vary_headers=["accept-language"])`),
or a user would get the cached response in another user's language.

## Pagination

Three tools, with nothing but FastAPI and pydantic: `FastAPIPagination` (`?page=2&size=20`),
`FastAPIPaginationLimitOffset` (`?limit=20&offset=40`) and `FastAPIPaginationCursor`
(`?size=20&cursor=...`). Each is configured once and used as a dependency, which reads and checks
the query parameters; the endpoint gets them as an argument, so `@cache` keeps each page under a
key of its own:

```python
from fastapi_alcazaba import FastAPIPagination
from fastapi_alcazaba.pagination import Page, PageParams

pages = FastAPIPagination(default_size=20, max_size=100)


@app.get("/products")
@cache(expire=60, tags=["products"])
async def products(params: Annotated[PageParams, Depends(pages)]) -> Page[Product]:
    return pages.paginate(await load_products(), params)
```

`GET /products?page=2&size=20` answers `{"items": [...], "total": 245, "page": 2, "size": 20,
"pages": 13}`.

- **From a database:** query only the page and give the total:
  `pages.paginate(rows, params, total=count)`, with `params.offset` and `params.limit` in the
  query. Without `total`, `paginate` slices the page from the whole list.
- **Limit and offset:** `FastAPIPaginationLimitOffset(default_limit=..., max_limit=...)` gives
  `LimitOffsetParams` and makes `LimitOffsetPage`s, the same way.
- **Limits:** each tool has its own (`max_size`, `max_limit`; 100 by default), so that nobody asks
  for a million rows at once: anything beyond, or malformed, is a 422 before the endpoint runs.
  They're in the OpenAPI schema too.
- **Coming from fastapi-pagination:** the JSON is that of its `Page` and `LimitOffsetPage`, so
  clients don't change. Its `add_pagination` keeps the page parameters out of the endpoint's
  arguments, in a context variable: a cache can't tell the pages apart, and fastapi-cache2 served
  page 1 for every page.

### By cursor

A cursor points at the last item of a page by its sort key, not by its position: rows added or
deleted meanwhile don't shift the pages (no item skipped or seen twice), and a database finds any
page through an index, as fast as the first. For infinite scroll, feeds and clients that sync
everything:

```python
from fastapi_alcazaba import FastAPIPaginationCursor
from fastapi_alcazaba.pagination import CursorPage, CursorParams

cursors = FastAPIPaginationCursor(secret=os.environ["CURSOR_SECRET"])  # secrets.token_urlsafe(32)


@app.get("/events")
@cache(expire=60)
async def events(params: Annotated[CursorParams, Depends(cursors)]) -> CursorPage[Event]:
    query = select(Event).order_by(Event.created_at, Event.id).limit(params.limit)
    if params.after is not None:  # (created_at, id) of the last event seen
        query = query.where(tuple_(Event.created_at, Event.id) > params.after)
    rows = (await session.scalars(query)).all()
    return cursors.paginate(rows, params, key=lambda e: (e.created_at, e.id), queried=True)
```

`GET /events?size=20` answers `{"items": [...], "size": 20, "next_cursor": "WyIyMDI2..."}`, and
`?cursor=<next_cursor>` the next page; `next_cursor` is null on the last one.

- **The sort key** (`key`) must be unique: `(created_at, id)`, not `created_at` alone, which two
  items may share. Its values can be strings, integers (up to 64 bits), floats, booleans,
  datetimes, dates, UUIDs and decimals, and `params.after` has them back as such.
- **Queried or sliced:** with `queried=True`, the rows are those of the page, queried with a key
  past `params.after` and `params.limit` (one more than `size`: it tells whether there's a next
  page). Without it, `paginate` finds the page in the whole sorted list. `descending=True` for
  newest first.
- **Signed:** with a `secret`, cursors carry an HMAC-SHA256 and a client can't make one up or
  change it; a list rotates secrets. Without it, a cursor's values are checked to be of those
  types. Either way an invalid cursor is a 400, and one longer than 1024 characters or malformed a
  422, before the endpoint runs. Cursors are base64, not encrypted: clients can read the sort key,
  so sort by what they may see.
- **Forward only:** to go back, clients keep the cursors they've used.

Every page, size and filter is one entry: tag them (`tags=["products"]`) to drop them all when the
data changes. For short lists, caching the whole list with `get_or_set` and slicing every page
from it keeps a single entry. The
[pagination example](https://github.com/nonodev96/fastapi-alcazaba/blob/main/examples/pagination/main.py)
does all three, with the database session left out of the key (`exclude_params`).

## Security model

- **What gets cached:** `GET` requests (any call for plain functions) whose response is a 200 and
  doesn't say `Cache-Control: no-store`.
- **Per-user data:** requests are told apart by the endpoint's arguments plus the
  `vary_headers`. If a response depends on anything else (`request.state`, a header that isn't in
  `vary_headers`, the client IP…), add it to `vary_headers` or to a custom key builder.
- **The backend is trusted for availability, not for integrity:** without a `signing_key` or an
  `encryption_key`, whoever can write to it decides what your endpoints return. Use one whenever
  the backend is shared or reachable by others, and always with `PickleCoder`.
- **Confidentiality:** signed entries are readable by whoever can read the backend, its dumps
  (RDB/AOF files) and backups. When responses carry personal or sensitive data, use
  `encryption_key` instead.
- **Keys** built by the default key builder are SHA-256 digests: arguments and tokens never
  appear in the backend in clear text.
- **Fail open:** if the backend is down or an entry can't be decoded, the endpoint runs as if
  there were no cache. Rate limiters let requests through too, unless `fail_open=False`.
- **Rate limits** count clients by the address the ASGI server gives, IPv6 by /64 (configure it to
  trust your proxy, and only it), or by your `identifier`. Whoever can write to the backend can
  reset them; `clear()` can't.

Report vulnerabilities as described in [SECURITY.md](https://github.com/nonodev96/fastapi-alcazaba/blob/main/SECURITY.md).

## Encrypted entries

```python
FastAPICache.init(
    RedisBackend(redis),
    prefix="myapp",
    encryption_key=os.environ["CACHE_ENCRYPTION_KEY"],  # secrets.token_urlsafe(32)
)
```

Each entry is encrypted with AES-256-GCM under a key derived (HKDF-SHA256) from the secret and a
random salt, with the cache key as associated data: an entry only decrypts under its own key, and
any change to it is detected. A list of secrets rotates them: the first encrypts, all of them
decrypt. Encryption also authenticates the entries, so it replaces `signing_key` rather than
adding to it. It costs a few microseconds per entry.

## Two-level cache

```python
from fastapi_alcazaba.backends.tiered import TieredBackend

FastAPICache.init(TieredBackend(ValkeyBackend(client), local_ttl=5), prefix="myapp")
```

`TieredBackend` keeps a small copy of each entry in the memory of every process, in front of the
shared backend: hits served from there skip the network (131 µs → 56 µs per hit against a Valkey
on the same machine; more across a real network). A local copy lives at most `local_ttl` seconds
and never longer than the shared entry.

With Redis or Valkey through redis-py/valkey-py, writes, clears and tag invalidations are also
announced on a pub/sub channel (`invalidation_channel`), so every process drops its copy at once:
a tag invalidation names the keys it deleted, also the copies of processes that only read those
entries (beyond 1000 keys, every process drops all its copies). A value read while an
invalidation arrives isn't kept, since it may be the old one. Copies are only kept while the
process is subscribed: if the subscription drops, or the client reconnects by itself, the local
level is emptied, and bypassed until it's back. The channel is read with its own timeout, so a
client `socket_timeout` doesn't end the subscription, and pinged after 5 quiet seconds: a
connection that died silently is given up within 10. With the other backends (memcached,
DynamoDB, GLIDE, a valkey-py cluster) staleness is bounded by `local_ttl`. If the shared backend
goes down, the local copies keep being served until they expire.

## Compression

```python
FastAPICache.init(RedisBackend(redis), compression="zstd")  # or "zlib", no dependencies
```

Entries of at least `compression_min_size` bytes (1 KiB) are compressed when that makes them
smaller; JSON lists shrink about 10-15 times with zstd, which means less memory in Redis/Valkey and
less network. zstd is in the standard library from Python 3.14 (`zstd` extra before). Compressed
entries are recognised when they're read whatever the setting, so it can be turned on or off at
any time; decompression is bounded by `max_value_size` (64 MiB if unset), so a compressed bomb
planted in the backend is rejected.

Compression happens before signing or encryption. With encryption, the size of an entry still
says how compressible it was: if an attacker can both read the backend and put chosen text into a
response next to a secret, that can leak information about the secret (the CRIME/BREACH family).
Leave compression off for such responses.

## Coders

`JsonCoder` (the default) stores anything FastAPI can turn into JSON, through pydantic-core
(Rust) when it can: about 8 times faster than fastapi-cache2 for a list of models. Annotate the
return type and cache hits come back as that type (Pydantic models, dataclasses, lists of
models…), validated straight from the stored bytes:

```python
@app.get("/products")
@cache(expire=60)
async def products() -> list[Product]: ...
```

`PickleCoder` stores any picklable object. Unpickling runs code chosen by whoever wrote the
bytes: use it only with a `signing_key` or an `encryption_key` (Alcazaba warns otherwise).

A custom coder subclasses `fastapi_alcazaba.Coder`:

```python
import orjson
from fastapi.encoders import jsonable_encoder

from fastapi_alcazaba import Coder


class ORJsonCoder(Coder):
    @classmethod
    def encode(cls, value: Any) -> bytes:
        return orjson.dumps(value, default=jsonable_encoder)

    @classmethod
    def decode(cls, value: bytes) -> Any:
        return orjson.loads(value)
```

## Key builders

The default key is the SHA-256 of the function's module and qualified name and the `repr()` of
its arguments, under `<prefix>:<namespace>:`. Arguments whose `repr()` changes on every call (a
database session or a service injected with `Depends`, whose `repr()` shows a memory address)
make every call a miss: Alcazaba warns about them (`UnstableCacheKeyWarning`) on the first call.
Leave them out with `exclude_params`:

```python
@app.get("/products")
@cache(expire=30, exclude_params=["session"])
def products(session: Annotated[Session, Depends(get_session)], page: int = 1) -> list[Product]: ...
```

A custom key builder receives the function, the namespace and the request:

```python
from typing import Any

from starlette.requests import Request
from starlette.responses import Response


def request_key_builder(
    func: Any,
    namespace: str = "",
    *,
    request: Request | None = None,
    response: Response | None = None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    assert request is not None
    return ":".join(
        [
            namespace,
            request.method.lower(),
            request.url.path,
            repr(sorted(request.query_params.items())),
        ]
    )


@app.get("/")
@cache(expire=60, key_builder=request_key_builder)
async def index() -> dict[str, str]:
    return {"hello": "world"}
```

Whatever the key builder, Alcazaba appends a digest of the `vary_headers`.

## Backends

- **InMemoryBackend(max_size=10_000):** per process; for development, tests and single-process
  apps. Up to `max_size` cached responses, and as many keys of state (counters, revoked tokens…)
  in a store of their own.
- **RedisBackend(redis):** redis-py client, standalone or cluster, created without
  `decode_responses=True`. Works with any server that speaks the Redis protocol (Valkey, KeyDB,
  Dragonfly).
- **ValkeyBackend(valkey):** the same with valkey-py (`valkey.asyncio`, not the sync client).
- **GlideBackend(client):** Valkey GLIDE, the official Valkey client with a Rust core
  (`await GlideClient.create(...)` or `GlideClusterClient`).
- **TieredBackend(remote, local_ttl=5):** a per-process memory level in front of any of these
  (see "Two-level cache").
- **MemcachedBackend(client):** aiomcache. Can't tell the remaining time to live.
- **DynamoBackend(table_name, region, endpoint_url=None):** aiobotocore; `await backend.init()`
  first. Enable TTL on the `ttl` attribute of the table. `endpoint_url` for DynamoDB Local or
  LocalStack.

| Backend | Clear a namespace | Locks | Tags | Rate limiter |
| --- | --- | --- | --- | --- |
| InMemory | yes | (one process) | yes | (one process) |
| Redis, Valkey, GLIDE | yes (`SCAN` + `UNLINK`) | yes | yes | yes, one Lua script |
| Memcached | no | no | no | yes, adding and taking back what doesn't fit |
| DynamoDB | no | yes (conditional writes) | no | yes, a conditional write |
| Tiered | like its shared backend | like its shared backend | like its shared backend | in its shared backend |

Valkey example with valkey-py:

```python
import valkey.asyncio

from fastapi_alcazaba.backends.valkey import ValkeyBackend

client = valkey.asyncio.from_url("valkey://localhost:6379/0")
FastAPICache.init(
    ValkeyBackend(client), prefix="myapp", signing_key=os.environ["CACHE_SIGNING_KEY"]
)
```

## Development

```shell
uv sync --all-extras
uv run pytest                 # unit tests
make services-up              # Redis, Valkey, a Valkey cluster, memcached and DynamoDB Local
make test-integration         # every backend against those servers
make services-down
make mutation                 # mutation testing (slow; CI runs it weekly)
make lint                     # ruff, ruff format --check, mypy --strict
```

Tests run in a random order (pytest-randomly; the seed is printed to reproduce a failure), and CI
fails a pull request with a benchmark more than 50 % slower than main's, compared with the others
so the speed of the runner doesn't count. See
[CONTRIBUTING.md](https://github.com/nonodev96/fastapi-alcazaba/blob/main/CONTRIBUTING.md).

## License and credits

Apache License 2.0. Alcazaba is a fork of fastapi-cache by long2ice and its contributors; see
[NOTICE](https://github.com/nonodev96/fastapi-alcazaba/blob/main/NOTICE). The name comes from the Andalusian fortresses — a cache that guards your
endpoints.
