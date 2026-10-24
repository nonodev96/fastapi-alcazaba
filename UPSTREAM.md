# Upstream

Alcazaba forked [long2ice/fastapi-cache](https://github.com/long2ice/fastapi-cache) at
`b2143ef` (2025-06-30). This repository starts a new history from there; the original one stays
in the upstream repository.

## Open pull requests (reviewed 2026-10-24)

| PR | Title | Status in Alcazaba |
| --- | --- | --- |
| [#182](https://github.com/long2ice/fastapi-cache/pull/182) | `respect_headers` to ignore Cache-Control | Reworked as `respect_cache_control`, **off by default** (clients could otherwise bypass the cache at will), with the directives parsed |
| [#231](https://github.com/long2ice/fastapi-cache/pull/231) | Type hint `@cache` for sync functions | Included |
| [#264](https://github.com/long2ice/fastapi-cache/pull/264) | BLAKE2 instead of MD5 | Superseded: SHA-256 |
| [#272](https://github.com/long2ice/fastapi-cache/pull/272) | libsql backend | Not included for now: niche, and a backend is easy to keep out of tree |
| [#294](https://github.com/long2ice/fastapi-cache/pull/294) | FastAPI version for Pydantic v2 | Superseded by #576 |
| [#338](https://github.com/long2ice/fastapi-cache/pull/338) | MD5 `usedforsecurity=False` | Superseded: SHA-256 |
| [#340](https://github.com/long2ice/fastapi-cache/pull/340) | `fastapi._compat.ModelField` on Pydantic 2 | Superseded by #576 (no private FastAPI API) |
| [#361](https://github.com/long2ice/fastapi-cache/pull/361) | Fix the `request_key_builder` docs | Included in the README |
| [#373](https://github.com/long2ice/fastapi-cache/pull/373) | no-store / no-cache in Cache-Control | Declined: it copied the client's Cache-Control directives into the response. Parsing is part of `respect_cache_control` |
| [#390](https://github.com/long2ice/fastapi-cache/pull/390) | Response model missing in OpenAPI | Included, through `Signature.replace` instead of a private attribute |
| [#427](https://github.com/long2ice/fastapi-cache/pull/427) | Pydantic models with invalid data | Declined: `construct()` skips validation and it used Pydantic internals. Entries that don't validate are recomputed instead |
| [#435](https://github.com/long2ice/fastapi-cache/pull/435) | Tag-based invalidation | Reworked: its tag lists were read-modify-write (racy) and grew without bound. Alcazaba uses server-side sets updated by a parameterised script, renamed before they're invalidated |
| [#445](https://github.com/long2ice/fastapi-cache/pull/445) | Valkey backend | Included (`ValkeyBackend`), without the Lua injection it copied from the Redis backend |
| [#455](https://github.com/long2ice/fastapi-cache/pull/455) | stale-while-revalidate | Included |
| [#477](https://github.com/long2ice/fastapi-cache/pull/477) | Memcached clear, `continue_on_error` | Partly: deleting one key is included; `flush_all` is declined (it wipes other applications' data). Backend errors already fail open |
| [#510](https://github.com/long2ice/fastapi-cache/pull/510) | Release v0.2.3 | Superseded by the new tooling; its DynamoDB `close()` fix is included |
| [#529](https://github.com/long2ice/fastapi-cache/pull/529) | `clear(key=...)` cleared the namespace | Included |
| [#546](https://github.com/long2ice/fastapi-cache/pull/546) | Cache POST requests | Declined: caching non-idempotent methods by default is unsafe |
| [#563](https://github.com/long2ice/fastapi-cache/pull/563) | Dogpile (stampede) prevention | Reworked: `single_flight` per process, `distributed_lock` across processes (`SET NX PX` and a token-checked release) |
| [#567](https://github.com/long2ice/fastapi-cache/pull/567) | Redis DriverInfo | Not included: it rewrote the settings of the caller's connection pool |
| [#569](https://github.com/long2ice/fastapi-cache/pull/569) | Deterministic ETag | Included, with SHA-256 and a quoted tag |
| [#572](https://github.com/long2ice/fastapi-cache/pull/572) | Modernise dependencies | Superseded: Python 3.10+, uv, current redis/valkey |
| [#573](https://github.com/long2ice/fastapi-cache/pull/573) | jinja2 as a dependency | Reworked: starlette.templating is imported lazily, no new dependency |
| [#574](https://github.com/long2ice/fastapi-cache/pull/574) | `exclude_params` | Included |
| [#575](https://github.com/long2ice/fastapi-cache/pull/575) | Don't cache error responses | Included, extended to statuses set on the injected response and to `no-store` |
| [#576](https://github.com/long2ice/fastapi-cache/pull/576) | Decode as the annotated return type | Included |

The ten open Dependabot pull requests are superseded by `uv.lock`.

## Unreleased upstream branches

- `integration` (2024-11): the `Callable[P, R]` type hints (#430) and the headers for custom
  responses (#437) are included; memcached `delete` (#452) is included without its `flush_all`.
- `release-v0.2.3` (2025-01): the DynamoDB fix for aiobotocore 2.18 (#509) is included; the rest
  were dependency and CI updates.

## Roadmap

Done since the fork: tag-based invalidation, distributed single flight, a size limit for values,
OpenTelemetry metrics, backend timeouts with a circuit breaker, early refresh and Valkey GLIDE.

Ideas for later:

- Compression of large entries.
- An instance-based API (one cache object per app) next to the global `FastAPICache`.
- Serving stale entries while they're refreshed in the background, for functions that don't
  depend on request-scoped resources.
