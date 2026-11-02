# Contract: `@cache` after this feature

## New parameter

```python
@cache(expire=60, cache_control="public, s-maxage=600")
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `cache_control` | `str \| None` | `None` | directives sent on miss and hit instead of the endpoint's or the computed ones; `max-age` is added (the entry's remaining lifetime) unless given; per-user entries are still `private` |

Invalid values raise `ValueError` when decorating: a line break, or `no-store` (use the endpoint's
own `no-store`, which skips the cache, or don't cache it).

## Behaviour guaranteed

1. A hit returns the status, body bytes and storable headers of the miss, for:
   - a returned `Response` with an in-memory body (JSON, HTML, text, binary), any coder;
   - a returned value plus the headers the endpoint added to the injected response.
2. Never stored or replayed: `Set-Cookie`, hop-by-hop headers, headers written on the injected
   response before the endpoint ran (dependencies), the cache's own headers, `Date`; background
   tasks.
3. Not stored: streaming and file responses, responses with `no-store` or `Set-Cookie`, statuses
   other than 200.
4. With a FastAPI route and a response model, the entry holds only what the model has; the
   route's include/exclude options apply to each response, hit or miss.
5. `Cache-Control`: configured directives > the endpoint's own > today's computed header; per-user
   entries never `public`.
6. 304s carry the entry's `Cache-Control`, `Expires`, `Vary`, `Content-Location`.
7. Entries of 0.7.x are served as before; 0.7.x recomputes entries of this version.

## Unchanged

`Coder` and its subclasses, `FastAPICache.init`, `Cache.cached`, `get_or_set`, keys, tags,
`clear`, signing, encryption, compression, size limits, single flight, distributed lock, early
refresh, `Cache-Status`, `ETag`, `Vary`.
