# Alcazaba

**A secure, maintained response cache for FastAPI.**

Alcazaba (`fastapi-alcazaba`) is a fork of [fastapi-cache](https://github.com/long2ice/fastapi-cache)
(`fastapi-cache2` on PyPI), whose last release dates from July 2024. It keeps the same API — you
change the imports — and fixes the security problems of the original:

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

And more: invalidation by tag, early refresh of popular entries, OpenTelemetry metrics, Valkey
through valkey-py or Valkey GLIDE, and the fixes that waited in upstream pull requests: cache hits
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

With the `otel` extra and an OpenTelemetry SDK configured, Alcazaba records a span per cached call
(`fastapi_alcazaba <function>`, with `fastapi_alcazaba.result`: hit, miss, refresh or bypass) and, inside it, one
per backend call (`fastapi_alcazaba.backend get`, `set`, `lock`…, with failures recorded). Instrumented
Redis/Valkey clients nest their own spans inside. And these metrics:

| Metric | Attributes |
| --- | --- |
| `fastapi_alcazaba.lookups` | `fastapi_alcazaba.result` (hit, miss, refresh, bypass), `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.stores` | `fastapi_alcazaba.outcome` (stored, uncacheable, too_large, unencodable, failed), `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.entry.size` | `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.backend.duration` | `fastapi_alcazaba.operation` (get, set, lock, unlock, tag) |
| `fastapi_alcazaba.backend.errors` | `fastapi_alcazaba.operation`, `error.type` |

## Responses in several languages

If an endpoint translates its response according to a request header, such as
`Accept-Language`, add that header to `vary_headers` (`@cache(vary_headers=["accept-language"])`),
or a user would get the cached response in another user's language.

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
  there were no cache.

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
announced on a pub/sub channel (`invalidation_channel`), so every process drops its copy at once.
Copies are only kept while the process is subscribed: if the subscription drops, the local level
is emptied and bypassed until it's back. With the other backends (memcached, DynamoDB, GLIDE, a
valkey-py cluster) staleness is bounded by `local_ttl`. If the shared backend goes down, the local
copies keep being served until they expire.

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
  apps.
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

| Backend | Clear a namespace | Locks | Tags |
| --- | --- | --- | --- |
| InMemory | yes | (one process) | yes |
| Redis, Valkey, GLIDE | yes (`SCAN` + `UNLINK`) | yes | yes |
| Memcached | no | no | no |
| DynamoDB | no | no | no |
| Tiered | like its shared backend | like its shared backend | like its shared backend |

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
