# Research: Whole cached responses and the endpoint's own Cache-Control

Findings from reading `fastapi_alcazaba/response_cache/` (decorator.py, coder.py, caches.py),
FastAPI 0.161.1 (`fastapi/routing.py`) and Starlette 1.9.0 (`starlette/responses.py`), and from
reproductions run on 2026-11-02.

## R1. Where the whole response is stored: an envelope in the decorator, not a new coder

- **Decision**: the decorator wraps what it stores in a small versioned envelope (magic prefix,
  one line of JSON metadata, then the raw bytes) whenever there is more than a bare value to keep:
  a returned `Response`, headers the endpoint set on the injected response, or a value rendered
  through the route's response model. A bare value with nothing else is stored exactly as today,
  through the configured coder.
- **Rationale**:
  - FR-002 asks for the default coder to do it with no configuration; doing it in the decorator
    makes it work with every coder (JSON, pickle, custom ones) and keeps `Coder` subclasses'
    contract unchanged.
  - A returned `Response` never goes through pickle any more, so its background tasks and cookies
    are never pickled and replayed (FR-003, FR-004), whatever the coder.
  - The most common case (a typed value with no headers) keeps its byte format: no mass miss on
    upgrade, and entries written by 0.7.x keep being served (FR-010).
  - Binary framing (metadata line + raw bytes) avoids base64 for bodies and for pickled values.
- **Alternatives considered**:
  - A `ResponseCoder` subclass used by default: coders see only the result, not the injected
    response or the route, so headers and response models would still need decorator support; and
    replacing the default coder changes the format of every entry.
  - Base64 in JSON for everything: simpler, 33 % larger entries.

## R2. Telling an envelope from a legacy entry, both ways

- **Decision**: the envelope starts with a fixed magic (`b"\x00alcazaba-entry:"` + version + `\n`).
  Reading: magic present → parse the envelope (an unknown version or a malformed one → recompute);
  absent → decode as today.
- **Rationale**: JSON never starts with NUL; pickle starts with `\x80`. A 0.7.x process reading a
  new entry fails to decode it (invalid JSON or unpickling error) and recomputes, which it already
  handles ("can't be decoded"): rolling deploys are safe in both directions.
- **Alternatives considered**: a separate key prefix for new entries (would double the entries
  during a deploy and break `clear(key=...)` users).

## R3. Which headers of the injected response are the endpoint's

- **Decision**: snapshot the injected response's raw headers just before the endpoint runs; after
  it returns, the headers added since then are the endpoint's. Only those are stored.
- **Rationale**: dependencies run before the endpoint and may write per-request headers on the same
  injected response: `accounting/meter.py:499` appends `RateLimit` and `RateLimit-Policy`. Storing
  them would replay one request's remaining quota to everybody.
- **Alternatives considered**: storing every header (replays per-request values); an allow-list of
  header names (can't anticipate the app's own headers).

## R4. Headers never stored

- **Decision**: drop `Set-Cookie` (such responses aren't stored at all since 0.7.1), hop-by-hop
  headers (`Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `TE`,
  `Trailer`, `Transfer-Encoding`, `Upgrade`), `Content-Length` (recomputed from the body), the
  cache's own (`Cache-Status`, `ETag`, `Age`) and `Date`; and `Link` when the call is paginated
  (`_add_page_links` rebuilds it from each request, by design).
- **Rationale**: RFC 9111 §3.1 (hop-by-hop not stored), §7.1 (`Set-Cookie`); the pagination rule
  already exists (decorator.py, `_add_page_links`).

## R5. Responses that can't be stored

- **Decision**: store a returned `Response` only if it has an in-memory `body` (bytes); streams
  (`StreamingResponse` and subclasses) and file responses (`FileResponse` has no `body`) are left
  uncached, as streams are today.
- **Rationale**: FR-005; caching a `FileResponse` would store an empty body.

## R6. Cache-Control precedence

- **Decision**: per target response, in order:
  1. `cache_control` directives configured on `@cache` (new parameter), plus `max-age=<remaining>`
     when they don't set `max-age`, plus `stale-while-revalidate` when that parameter is set;
  2. else the endpoint's own `Cache-Control` (set on the injected response or on the returned
     `Response`, stored and replayed), kept verbatim;
  3. else today's computed header.

  For per-user entries, `public` is removed and `private` added in all three cases. `no-store` set
  by the endpoint still means "not stored" (unchanged).
- **Rationale**: the maintainer's answer (endpoint wins by default); FR-007/FR-008.
- **Alternatives considered**: a structured `CacheControl` object (more API than the string
  directives users already know); a global default in `FastAPICache.init` (not needed yet: one
  per endpoint, as `stale_while_revalidate` is).

## R7. Storing what the response model lets through

- **Decision**: when the request's scope carries the FastAPI route (`scope["route"]`, an
  `APIRoute`) with a response model, and the endpoint returned a value (not a `Response`), store the
  value validated through the model (`TypeAdapter(route.response_model)`,
  `validate_python(value, from_attributes=True)`) and dumped by alias, leaving unset fields out
  when the route says `response_model_exclude_unset`. The envelope marks it `rendered`; a hit
  validates it back into the response model (not the function's return annotation, which may be
  wider), and FastAPI renders it with the route's options, as for any return value.
- **Revised during implementation**: a first version stored the output with every route option
  applied and returned it as a dict on hits. Two failures: pagination needs the page object to
  build its links (7 tests), and FastAPI validates every returned value against the response
  model, so an output without a required field the route excludes would make each hit a 500.
  `include`, `exclude`, `exclude_none` and `exclude_defaults` are therefore left to FastAPI on each
  response; only `exclude_unset`, which never drops a field without a default, is applied.
- **Rationale**: FR-009; reproduction on 2026-11-02 stored `"password_hash"` for an endpoint whose
  response model drops it. `TypeAdapter` and the route attributes are public API; FastAPI's own
  `serialize_response` is internal and changed signature across versions.
- **Fallbacks**: no route in scope (old Starlette, or a function cached outside a route) → the
  value is stored as today. A value that doesn't validate → not stored (FastAPI raises
  `ResponseValidationError` on that request anyway).
- **Alternatives considered**: calling `fastapi.routing.serialize_response` (internal API);
  filtering only when the response model differs from the return annotation (a returned subclass
  instance would still leak its extra fields).

## R8. 304 responses

- **Decision**: when `If-None-Match` matches, the 304 also carries the stored entry's
  `Cache-Control`, `Expires`, `Vary` and `Content-Location` (RFC 9110 §15.4.5), which means reading
  the envelope's metadata before answering.
- **Rationale**: a 304 without the endpoint's `Cache-Control` would let a CDN fall back to its
  defaults.

## R9. Performance

- The bare-value path is unchanged. The envelope adds a metadata line (tens of bytes) and one
  `bytes.split` on read; the snapshot of the injected response's headers is a list copy per miss.
  The benchmarks gate (`scripts/bench_gate.py`) covers the hit path.
