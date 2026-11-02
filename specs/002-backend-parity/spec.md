# Feature Specification: Every backend can do what the tools need

**Feature Branch**: `feature/backend-parity`

**Created**: 2026-11-02

**Status**: Draft

**Input**: User description: "Every backend should support the features the other tools rely on,
or refuse clearly when it can't: locks, clearing a namespace or tag, and knowing an entry's
remaining TTL. Today memcached has no locks, so distributed single-flight silently does nothing,
the concurrency limiter, idempotency keys and TOTP replay protection can't be used on it; memcached
and DynamoDB can't clear a namespace or invalidate by tag; and memcached hits lose their TTL, so
hits carry no max-age and early refresh silently never happens."

## Clarifications

### Session 2026-11-02

No question was put to the maintainer, who asked for the feature to be built and merged. The
choices a reader might have asked about are recorded, with their reasons, in research.md:

- How many entries a clear deleted can't be counted without listing keys: memcached and DynamoDB
  answer 0 (R3).
- memcached keeps time in whole seconds: its locks last at least what was asked, rounded up to the
  next second plus one (R1).
- Redis, Valkey and GLIDE keep `SCAN` + `UNLINK` for namespaces, and their tag sets: nothing
  changes on them (R6).
- Cached responses stored in memcached by 0.7 are recomputed once after upgrading (R5).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Locks on memcached (Priority: P1)

An app runs on memcached, because it's what its platform offers. Today `distributed_lock` warns
and does nothing, `Lock`, the concurrency limiter, `IdempotencyMiddleware` and `FastAPITOTP`
refuse the backend, and user sessions are indexed without their lock.

With this feature, memcached holds locks with the guarantees of Redis: one holder at a time, the
lock expires if its holder dies, and only the holder releases or extends it.

**Why this priority**: locks protect the security tools (a TOTP code accepted once, a request run
once); without them those tools can't be used at all.

**Independent Test**: on a real memcached, many contenders for one lock get it once; a release
or an extension with another token does nothing; the lock frees itself after its time.

**Acceptance Scenarios**:

1. **Given** memcached, **When** ten processes try to take one lock at once, **Then** exactly one
   gets it.
2. **Given** a lock held by one holder, **When** another releases or extends it with its own
   token, **Then** nothing changes, and the holder still holds it.
3. **Given** a holder that never releases, **When** its time is up, **Then** someone else can take
   the lock; never before the time asked.
4. **Given** memcached, **When** `FastAPITOTP`, `FastAPIConcurrencyLimiter`, `Lock`,
   `IdempotencyMiddleware` and `distributed_lock` are set up, **Then** none refuses or warns, and
   each behaves as on Redis.

---

### User Story 2 - Clearing a namespace or a tag on memcached and DynamoDB (Priority: P2)

An app caches product pages on DynamoDB. When a product changes, it calls
`FastAPICache.invalidate_tags("product:42")`, or `FastAPICache.clear(namespace="products")` after
a bulk import. Today both raise `NotImplementedError`, and the app has to wait for the entries to
expire.

With this feature, both work on memcached and DynamoDB: after the call, no entry of that namespace
or tag is served again, without listing keys.

**Why this priority**: invalidation decides whether stale data is served; it's the second most
visible gap.

**Independent Test**: store entries in two namespaces, one tagged; clear one namespace and
invalidate the tag; check which entries are still served.

**Acceptance Scenarios**:

1. **Given** entries in namespaces `a` and `ab`, **When** `a` is cleared, **Then** no entry of `a`
   is served again and the entries of `ab` are.
2. **Given** entries tagged `t` and others tagged `u`, **When** `t` is invalidated, **Then** no
   entry tagged `t` is served again, and those tagged `u` are.
3. **Given** a clear while an entry is being computed, **When** the computation stores it,
   **Then** that entry is not served either.
4. **Given** `clear()` without arguments, **When** called, **Then** every cached response of the
   cache's prefix is gone, and its state (locks, counters, revocations, sessions) is kept.

---

### User Story 3 - memcached hits know their remaining lifetime (Priority: P3)

Today a memcached hit can't tell how long its entry has left: the response carries no `max-age`
countdown and `early_refresh` never refreshes.

With this feature, a memcached hit knows its remaining lifetime like the other backends.

**Why this priority**: it's about freshness headers and refresh, not correctness of data.

**Independent Test**: store an entry for 60 seconds, read it back, check the remaining lifetime.

**Acceptance Scenarios**:

1. **Given** an entry stored for 60 seconds on memcached, **When** it's read, **Then** its
   remaining lifetime is between 1 and 60 seconds.
2. **Given** `early_refresh` on memcached, **When** an entry is read near its end, **Then** it's
   refreshed as on Redis.

---

### Edge Cases

- A namespace or tag cleared again and again: each clear works; nothing grows with the number of
  clears.
- memcached evicts the record of a namespace's clears: the entries under it are recomputed, never
  served stale.
- A namespace deeper than the backend tracks: the clear is refused with a clear error, never
  silently ignored (R2).
- A clear seen by a DynamoDB reader a moment late (eventually consistent reads): an entry may be
  served for that moment, as with any read of DynamoDB (R4).
- Bytes read from memcached that aren't an entry of this version (another app, a corrupt value, an
  entry of 0.7): never an error; a miss.
- Counters, locks and other state on the same backend: unaffected by any clear, as today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: memcached MUST take, release and extend locks atomically: one holder at a time, only
  the holder releases or extends it, and it expires on its own.
- **FR-002**: A memcached lock MUST NOT expire before the time asked; it may last up to two seconds
  more (memcached counts whole seconds).
- **FR-003**: memcached and DynamoDB MUST clear a namespace (`clear(namespace=...)`) and invalidate
  tags (`invalidate_tags`), without listing keys.
- **FR-004**: After a clear or an invalidation returns, no entry stored under that namespace or tag
  before it MUST be served again from memcached, nor from DynamoDB once its reads catch up.
- **FR-005**: An entry stored by a computation that overlapped a clear MUST NOT be served after it.
- **FR-006**: memcached MUST report the remaining lifetime of an entry.
- **FR-007**: Clears MUST never delete or hide state: locks, counters, revocations, sessions,
  one-time codes, idempotency records.
- **FR-008**: Where a backend can't do something (a backend written by the app), the tool that
  needs it MUST refuse when it's set up, saying that every backend shipped with fastapi-alcazaba
  can, and never weaken a security guarantee without saying so.
- **FR-009**: Nothing MUST change on InMemory, Redis, Valkey and GLIDE.
- **FR-010**: Values memcached or DynamoDB hold that this version didn't write MUST be served or
  treated as a miss, never raise.
- **FR-011**: The backends table of `docs/guide/backends.md` and the pages that say memcached can't
  hold locks MUST say what the backends do after this change.

### Key Entities

- **Generation**: a random token kept per namespace and per tag that was ever cleared; replaced by
  each clear.
- **Stamp**: what an entry records with its value on memcached and DynamoDB: its expiry, and the
  generations of its namespaces and tags when it was stored.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The live backend tests run locks, namespace clears, tag invalidations and remaining
  lifetimes on every backend, none skipped for lack of a capability.
- **SC-002**: `FastAPITOTP`, the concurrency limiter, `Lock`, idempotency keys and
  `distributed_lock` run their live tests on memcached.
- **SC-003**: The unit and integration suites of InMemory, Redis, Valkey and GLIDE pass unchanged.
- **SC-004**: A memcached hit costs one round trip (two for a tagged entry); a DynamoDB hit one
  request (two for a tagged entry).

## Assumptions

- Entries of a cache are stored under keys whose namespaces are separated by `:`, as `Cache` and
  the default key builder do; state keys (`Cache.state_key`) aren't in any namespace.
- memcached may evict anything; an eviction may cost recomputations, never correctness.
- DynamoDB tables keep their configuration (a `key` hash key, TTL on `ttl`): nothing to migrate.

## Out of Scope

- Generations on Redis, Valkey and GLIDE: their `SCAN` frees the memory at once and counts what it
  deleted; a cluster's `SCAN` is per node and works. The prompt's "does not need SCAN on Redis
  clusters" is left for a change of its own.
- Counting what a clear deleted on memcached and DynamoDB.
