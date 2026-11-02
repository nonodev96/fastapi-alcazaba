# Feature Specification: Whole cached responses and the endpoint's own Cache-Control

**Feature Branch**: `feature/cached-response-coder`

**Created**: 2026-11-02

**Status**: Draft

**Input**: User description: "Cached responses should come back exactly as the endpoint produced
them, and endpoints should control how clients and CDNs cache them. Today, with the default JSON
coder, a hit returns only the body: headers the endpoint set are lost, and a non-JSON Response
can't be stored at all. The Cache-Control header is always rewritten by the cache. What's stored
should be what the client received."

## Clarifications

### Session 2026-11-02

- Q: Should the default coder store whole responses automatically, or only a new opt-in coder?
  → A: The default coder stores them: apps get it on upgrade; entries in the old format miss once
  and are recomputed.
- Q: When the endpoint sets its own `Cache-Control` and nothing is configured, who wins? → A: The
  endpoint: its header is kept on miss and hit; the cache sets its own only when the endpoint sets
  none. Per-user entries are still forced `private`.
- Q: Is storing only the response model's output part of this feature? → A: Yes.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A hit returns the response the endpoint produced (Priority: P1)

An API author caches an endpoint that returns a response object: a JSON response with its own
headers (`X-Total-Count`, `Content-Language`), an image, a CSV export or an HTML page. Today, with
the default coder, a hit returns only the body of a JSON response, as a 200 with the default
headers, and any non-JSON response is never stored, so every request reaches the endpoint. The
author has to switch to pickle with a signing key to cache it at all.

With this feature, the hit returns the same status, the same body bytes and the same headers as
the miss did, for JSON and non-JSON responses alike, with the default coder and without pickle.
Headers that belong to one client (a cookie) or to one connection are never stored.

**Why this priority**: it's the core gap. A cache that silently never caches a response, or
drops the headers clients depend on (pagination totals, content language, content type), gives
wrong answers or no benefit, and today the only way around it is the coder that runs code from the
backend.

**Independent Test**: cache an endpoint that returns a PNG, another that returns a JSON response
with a custom header, and compare the miss and the hit byte for byte and header by header.

**Acceptance Scenarios**:

1. **Given** an endpoint returning a non-JSON response (an image), **When** it's requested twice,
   **Then** the second request is a hit and returns the same bytes and content type, and the
   endpoint ran once.
2. **Given** an endpoint returning a JSON response with a custom header, **When** it's requested
   twice, **Then** the hit carries that header with the same value.
3. **Given** an endpoint that sets a header on the injected response and returns a value,
   **When** it's requested twice, **Then** the hit carries that header too.
4. **Given** an endpoint whose response sets a cookie, **When** it's requested, **Then** nothing is
   stored (as since 0.7.1), and no stored entry ever carries a cookie or a hop-by-hop header.
5. **Given** entries stored by an earlier version, **When** they're read after upgrading, **Then**
   they're served as before or treated as a miss, never as an error.

---

### User Story 2 - The endpoint decides how clients and CDNs cache it (Priority: P2)

An operator puts a CDN in front of the API. Some endpoints should be `public` with a longer
shared lifetime (`s-maxage`), static assets `immutable`, others `no-cache` (always revalidate with
the ETag). Today the cache rewrites `Cache-Control` on every miss and hit to `max-age=<expire>`
(plus `private` per user), overwriting whatever the endpoint set.

With this feature, the endpoint can keep its own `Cache-Control`, or choose the directives through
the cache's options, and the hit sends the same policy as the miss.

**Why this priority**: it unlocks CDNs and browser caching correctly, but a wrong default here
never leaks data the way Story 1's gaps do.

**Independent Test**: an endpoint that sets `public, max-age=999` and one configured for
`no-cache`; check the header on miss and hit.

**Acceptance Scenarios**:

1. **Given** an endpoint that sets its own `Cache-Control`, **When** it's served from a miss and
   from a hit, **Then** both carry the endpoint's header (FR-006).
2. **Given** an endpoint configured with directives (`public`, `s-maxage`, `immutable`, `no-cache`),
   **When** it's served, **Then** the header carries them, and `max-age` still counts down with
   the entry's remaining lifetime unless the endpoint fixed it.
3. **Given** a per-user entry (a request with `Authorization` or `Cookie`), **When** it's served,
   **Then** it's never marked `public`, whatever the endpoint or the options say.
4. **Given** no endpoint header and no options, **When** served, **Then** the header is exactly
   what it is today.

---

### User Story 3 - What's stored is what the client receives (Priority: P3)

An endpoint declares a response model that leaves fields out (`password_hash`, internal flags) and
returns a richer object. Today the cache stores the raw return value, every field included, so the
backend holds data no client ever sees; clients get the filtered output only because the framework
filters again on every hit.

With this feature, the stored entry holds only the output the client receives.

**Why this priority**: clients aren't affected today; it's data at rest in a shared backend, which
an `encryption_key` already mitigates. Valuable, but after Stories 1 and 2.

**Independent Test**: an endpoint with a response model that drops a field; inspect the stored
entry.

**Acceptance Scenarios**:

1. **Given** an endpoint with a response model narrower than its return value, **When** it's
   cached, **Then** the stored entry doesn't contain the fields the model drops, and hits return
   the same output as misses.

---

### Edge Cases

- A response whose body isn't in memory (a file response, a stream): not stored, as streams aren't
  today.
- A response with background tasks attached: the tasks run on the miss only, never on a hit, and
  aren't stored.
- `HEAD` requests and conditional requests (`If-None-Match` → 304): behave as today.
- `Content-Length` on a hit always matches the body served; compression middleware placed after
  the cache keeps working.
- A response over the backend's or the configured size limit: not stored, as today.
- `Vary` set by the endpoint and by the cache: merged, as today, on miss and hit.
- An endpoint that sets `no-store` itself: not stored, as today.
- Entries written by 0.7.x and read by this version, and the reverse during a rolling deploy:
  never an error.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A hit MUST return the status, the body bytes and the headers that the miss returned,
  for responses returned by the endpoint and for headers set on the injected response, except the
  headers listed in FR-003.
- **FR-002**: Responses of any media type whose body is in memory MUST be stored whole by the
  default coder, without pickle and without any configuration.
- **FR-003**: `Set-Cookie`, hop-by-hop headers (`Connection`, `Keep-Alive`, `Transfer-Encoding`,
  `Upgrade`, `TE`, `Trailer`, `Proxy-*`) and the headers the cache itself sets (`Cache-Status`,
  `ETag`, `Age`) MUST NOT be stored; responses that set a cookie MUST stay uncached.
- **FR-004**: Background tasks attached to a response MUST run only when the endpoint runs, never
  on a hit.
- **FR-005**: Responses whose body isn't in memory (files, streams) MUST NOT be stored.
- **FR-006**: An endpoint MUST be able to keep its own `Cache-Control`, or set directives (`public`
  or `private`, `max-age`, `s-maxage`, `immutable`, `no-cache`, `must-revalidate`,
  `stale-while-revalidate`, `stale-if-error`) through the cache's options. When the endpoint sets
  its own header and no directives are configured, the endpoint's header MUST be kept, on miss and
  hit; configured directives take precedence over it.
- **FR-007**: Per-user entries MUST never be sent as `public`; `private` is enforced for them.
- **FR-008**: With no endpoint header and no new options, the `Cache-Control` sent MUST be exactly
  today's.
- **FR-009**: When the endpoint declares a response model, the stored entry MUST hold only the
  output the client receives.
- **FR-010**: Entries written in the previous format MUST be served as before or treated as a miss,
  never as an error; the stored format MUST be versioned so the next change can do the same.
- **FR-011**: Signing, encryption, compression, the size limit, tags, single flight and the
  distributed lock MUST work on whole responses as they do on values today.
- **FR-012**: Everything above MUST work on every backend.

### Key Entities

- **Cached response**: what a hit replays: status, the storable headers (name and value, in order,
  repeated names kept), the body bytes, and a format version. Signed or encrypted as configured.
- **Cache-Control policy**: the directives an endpoint sends on miss and hit: whether the
  endpoint's own header is kept, which directives are added, and how `max-age` relates to the
  entry's remaining lifetime.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every response type in the test matrix (JSON value, JSON response with headers,
  HTML, plain text, CSV, PNG), the second identical request is served from the cache, with the
  endpoint run once, and its status, body and storable headers are identical to the first.
- **SC-002**: No stored entry, in any test, contains a `Set-Cookie` or hop-by-hop header, or a
  field a response model drops.
- **SC-003**: Apps upgrading from 0.7.x keep answering every request: zero errors caused by entries
  in the previous format.
- **SC-004**: Every existing test of the response cache passes unchanged, except those that assert
  the behaviour this feature changes on purpose (listed in the plan).
- **SC-005**: An endpoint's `Cache-Control` policy is the same on miss and hit in 100% of the
  scenarios of Story 2.

## Assumptions

- Only successful responses (200) are stored, as today; caching other statuses is a separate
  change.
- Caching non-GET methods and streaming responses stays out of scope.
- The library is in 0.x: a change of default is acceptable when the changelog says what changes
  and how to keep the old behaviour.
- No new dependency is needed.
- Pickle keeps working for apps that use it today; this feature makes it unnecessary for responses,
  not forbidden.
