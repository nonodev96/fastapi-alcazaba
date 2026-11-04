# Response cache

`@cache` for endpoints and functions, and `Cache` behind it: what each entry is stored under, for
how long, and how it's kept safe. The [quick start](index.md#step-1-cache-an-endpoint) sets
one up.

In an app: [the example](../examples/cache.md). Every class and function: [the API reference](../reference/cache.md).

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
| `cache_status` | `"Alcazaba"` | this cache's name in the `Cache-Status` header (RFC 9211); `None` to leave it out |
| `enable` | `True` | `False` turns the cache off |
| `signing_key` | `None` | secret of 32+ bytes to sign entries; a list rotates keys (the first signs) |
| `encryption_key` | `None` | secret of 32+ bytes to sign *and encrypt* entries (`crypto` extra); instead of `signing_key` |
| `compression` | `None` | `"zstd"` or `"zlib"`: store large entries compressed |
| `compression_min_size` | `1024` | bytes; smaller entries aren't compressed |
| `compression_level` | codec's default | |
| `vary_headers` | `("authorization", "cookie", "x-api-key")` | request headers that give each value its own entry: add any other header the app reads credentials from |
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
| `stale_while_revalidate` | `None` | seconds past `expire` an entry is served at once and refreshed in the background (see [below](#serving-stale-entries)); also that `Cache-Control` directive |
| `stale_if_error` | `None` | seconds past `expire` an entry is served when computing a new one fails |
| `refresh_timeout` | `None` | seconds computing may take while `stale_if_error` can serve: past them, the stale entry |
| `cache_control` | `None` | the `Cache-Control` directives to send (see [below](#cache-control)) |
| `single_flight` | from `init` | |
| `max_value_size` | from `init` | |
| `distributed_lock` | from `init` | |
| `early_refresh` | from `init` | |
| `tags` | `None` | names to invalidate the entry by: a list, or a function of the arguments |

Only 200 responses are stored, and never one that says `no-store`, sets a cookie (its hits
would hand that cookie, a session, to everyone else), streams or sends a file.

### What a hit returns

The response the endpoint gave, whatever its type and whatever the coder:

- **A `Response` it returned** (JSON, HTML, CSV, an image…) comes back with its status, its
  headers and its body, byte for byte. Its background tasks run when the endpoint does, not on
  hits.
- **A value**, with the headers the endpoint set on the injected response
  (`response.headers["X-Total-Count"] = "2"`). Those its dependencies set first, before the
  endpoint ran (a remaining quota, a request ID), belong to each request: they're never stored.
- **With a response model**, only what the model has is stored: a field the endpoint returns and
  the model leaves out (a password hash) never reaches the backend. The route's
  `response_model_include`, `exclude` and `exclude_none` apply to each response, hit or miss.

`Set-Cookie`, the connection's own headers (`Connection`, `Transfer-Encoding`…) and the cache's
own (`ETag`, `Cache-Status`) are never stored, whoever writes the entry.

### Cache-Control

What each response is sent with, miss or hit:

1. `cache_control="public, s-maxage=600"` on `@cache`: those directives, with `max-age` the time
   the entry has left, unless they give one (`stale-while-revalidate` too, if set).
2. Else the endpoint's own `Cache-Control`, as it set it.
3. Else `max-age`, the time the entry has left (and `stale-while-revalidate`).

An entry of one user (a request with `Authorization`, `Cookie` or `X-API-Key`, see
`vary_headers`) is `private` whatever these say. A 304 repeats the response's `Cache-Control`, `Expires`, `Vary`
and `Content-Location`. `no-store` isn't a directive to configure: set on the response, it keeps
it out of the cache.

```python
@app.get("/logo.png")
@cache(expire=86_400, cache_control="public, immutable")
async def logo() -> Response:
    return Response(LOGO, media_type="image/png")
```

Delete entries with `await FastAPICache.clear(namespace="products")`, one entry with
`await FastAPICache.clear(key=...)`, or by tag with `await FastAPICache.invalidate_tags(...)`.
`clear()` without arguments deletes every cached response under the prefix. memcached and
DynamoDB can't list their keys: there, a clear or an invalidation hides the entries instead (they
expire in their own time) and returns 0, since it can't count them. Locks, rate-limit
counters, revoked tokens, one-time codes and idempotency records are kept apart, under
`{prefix}!` instead of `{prefix}:`, so no `clear()` frees a held lock, resets a limit or takes a
revocation back; `InMemoryBackend` keeps them apart too, so caching many responses never evicts
them.

## Serving stale entries

When a fresh value is slow or can't be had, an expired one is often better than waiting or an
error:

```python
@app.get("/prices")
@cache(expire=60, stale_while_revalidate=300, stale_if_error=3600, refresh_timeout=2)
async def prices(db: Annotated[AsyncSession, Depends(session)]) -> list[Price]: ...
```

- **stale-while-revalidate:** up to 300 seconds after it expires, the entry is served at once
  (`Cache-Status: Alcazaba; hit; ttl=-12; detail=stale-while-revalidate`), and the endpoint
  runs again after the response, to store a fresh one. One process does it (a lock in the
  backend, tried once), once (the other requests meanwhile get the stale entry too). In an
  endpoint, the refresh is a background task of the response: it runs once the response is
  sent, before the request's dependencies are closed, so the database session above is still
  open. A refresh that fails is logged; the next request tries again.
- **stale-if-error:** up to an hour after it expires, a request computes a fresh value as on a
  miss, but when the endpoint raises, or takes longer than `refresh_timeout` (it's cancelled
  then), the expired entry is served instead
  (`Cache-Status: Alcazaba; hit; ttl=-1800; detail=stale-if-error`). Past the window, the error
  goes through.
- **How:** entries are stored for `expire` plus the longest of both windows, and a read tells a
  fresh entry from a stale one by the time it has left. Signed, encrypted and compressed
  entries, tags, invalidation, single flight and the distributed lock work as for any entry: an
  invalidated entry isn't served stale.
- **Counted** in `fastapi_alcazaba.lookups` as `stale` and `stale_if_error`. Stale responses get
  no `max-age`: browsers and CDNs don't keep them as fresh.
- Without an `expire` (entries that never expire), nothing is ever stale.

## Several caches, dependency injection and `get_or_set`

`FastAPICache` is one global cache. For one cache per app, or several with different settings,
create `Cache` instances (same arguments as `FastAPICache.init()`) and decorate with their
`cached()`:

```python
from fastapi_alcazaba import Cache, get_cache

public_cache = Cache(RedisBackend(redis), prefix="public", vary_headers=())
private_cache = Cache(
    RedisBackend(redis), prefix="private", encryption_key=settings.cache_encryption_key
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

## Responses in several languages

If an endpoint translates its response according to a request header, such as
`Accept-Language`, add that header to `vary_headers` (`@cache(vary_headers=["accept-language"])`),
or a user would get the cached response in another user's language.

## Encrypted entries

```python
FastAPICache.init(
    RedisBackend(redis),
    prefix="myapp",
    encryption_key=settings.cache_encryption_key,  # secrets.token_urlsafe(32)
)
```

Each entry is encrypted with AES-256-GCM under a key derived (HKDF-SHA256) from the secret and a
random salt, with the cache key as associated data: an entry only decrypts under its own key, and
any change to it is detected. A list of secrets rotates them: the first encrypts, all of them
decrypt. Encryption also authenticates the entries, so it replaces `signing_key` rather than
adding to it. It costs a few microseconds per entry.

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
(Rust) when it can: about 8 times faster than `jsonable_encoder()` for a list of models. Annotate the
return type and cache hits come back as that type (Pydantic models, dataclasses, lists of
models…), validated straight from the stored bytes:

```python
@app.get("/products")
@cache(expire=60)
async def products() -> list[Product]: ...
```

`PickleCoder` stores any picklable object. Unpickling runs code chosen by whoever wrote the
bytes: it needs a `signing_key` or an `encryption_key`, and without one `FastAPICache.init`
(or the first call of an endpoint that asks for it) raises `UnsignedEntriesError`.

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
