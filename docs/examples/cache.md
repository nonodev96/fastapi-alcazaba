# Response cache

--8<-- "docs/examples/animations/cache.svg"

A shop's catalogue and account pages behind [`@cache`](../guide/index.md#step-1-cache-an-endpoint): product pages are
the same for everybody and share one entry each, account pages depend on who asks and get entries
of their own, an update drops the entries of a product by [tag](../guide/cache.md#invalidating-by-tag),
and [`get_or_set`](../guide/cache.md#several-caches-dependency-injection-and-get_or_set) caches what
doesn't fit a decorator. Every entry is signed, so whoever can write to Redis can't plant one.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.cache.main:app
```

It keeps the entries in memory. To share them between processes, give it a Redis or Valkey and a
signing key, the same in every process:

```bash
export REDIS_URL=redis://localhost:6379/0
export CACHE_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
uvicorn examples.cache.main:app --workers 4
```

## Try it

The first request computes the page and stores it, the next ones get it from the cache, as the
[`Cache-Status`](https://www.rfc-editor.org/rfc/rfc9211) header says, with the seconds the entry
has left:

```console
$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
etag: W/"8a7a39cf30d133a472a8c4f0beca023d"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=600

{"id":1,"name":"Olive oil","price":9.5}

$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
etag: W/"8a7a39cf30d133a472a8c4f0beca023d"
cache-status: Alcazaba; hit; ttl=600
cache-control: max-age=600

{"id":1,"name":"Olive oil","price":9.5}
```

A personal page is cached per `Authorization` header and marked private, so neither another user
nor a shared cache in between gets it:

```console
$ curl -i http://127.0.0.1:8000/me -H "Authorization: Bearer alice"
HTTP/1.1 200 OK
cache-status: Alcazaba; fwd=miss; stored
cache-control: private, max-age=30
vary: Authorization, Cookie

{"user":"alice"}
```

An update drops the product's entry, whatever its key:

```console
$ curl -X PUT http://127.0.0.1:8000/products/1 -H "Content-Type: application/json" \
    -d '{"id": 1, "name": "Olive oil", "price": 10}'
$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
cache-status: Alcazaba; fwd=miss; stored

{"id":1,"name":"Olive oil","price":10.0}
```

## What to look at

- **`vary_headers=()` only for public data:** by default a request with `Authorization` or
  `Cookie` gets an entry of its own, sent as `Cache-Control: private`. The product page opts out
  because it's the same for everybody; the account page keeps the default.
- **Tags instead of keys:** the product page is tagged `product:<id>`, so the update drops it
  (and anything else with that tag) without rebuilding its key.
- **Errors aren't cached:** the 404 of an unknown product is computed every time, never replayed.
- **`get_or_set` for the rest:** the recommendations are cached under a key the code chooses,
  through the same pipeline as the decorator (signing, single flight, locks).
- **A signing key from the environment:** the fallback in the code is only for trying it out.
  With Redis, every process must share the same key, or each refuses the others' entries.

## The code

<!-- fmt: off -->
```python title="examples/cache/main.py"
--8<-- "examples/cache/main.py"
```
<!-- fmt: on -->

The repository also has [`examples/redis`](https://github.com/nonodev96/fastapi-alcazaba/tree/main/examples/redis),
with Redis, signed entries, a sync endpoint run in the thread pool and an HTML page cached with
`PickleCoder`.
