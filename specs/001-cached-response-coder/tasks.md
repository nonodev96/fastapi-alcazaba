# Tasks: Whole cached responses and the endpoint's own Cache-Control

**Input**: Design documents from `specs/001-cached-response-coder/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/cache-decorator.md,
quickstart.md

**Tests**: required by the constitution (principle II): every story has tests, and each fix a test
that fails without it.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)

## Phase 1: Setup

- [ ] T001 Read the response cache tests that assert today's header and Response behaviour
  (`tests/unit/test_decorator.py`, `test_features.py`, `test_security.py`,
  `test_cache_status.py`) and list those this feature changes on purpose in plan.md's "Changes to
  existing behaviour"

## Phase 2: Foundational (blocking all stories)

- [ ] T002 Create `fastapi_alcazaba/response_cache/entries.py`: the envelope of data-model.md —
  `Entry` (kind, status, headers, payload), `encode_entry`, `parse_entry` (None for legacy bytes,
  raises `ValueError` for a malformed envelope), `storable_headers` (R4, with a `paginated` flag),
  `MAGIC`
- [ ] T003 [P] Unit tests for the envelope in `tests/unit/test_cached_responses.py`: round trip of
  each kind, repeated headers and order kept, legacy bytes → None, malformed/unknown version →
  ValueError, forbidden headers dropped both when encoding and when parsing
- [ ] T004 [P] Fuzz target `fuzz/entries_fuzzer.py` (parse arbitrary bytes: only ValueError, never a
  forbidden header) and its known cases in `tests/unit/test_fuzz_targets.py`

**Checkpoint**: the envelope exists and is tested; nothing uses it yet.

## Phase 3: User Story 1 — A hit returns the response the endpoint produced (P1) 🎯 MVP

**Goal**: status, body and the endpoint's headers come back on a hit, for any in-memory response,
with any coder; never cookies, hop-by-hop or dependencies' headers; no background tasks replayed.

**Independent Test**: PNG, CSV, HTML, JSONResponse with a header, and a value with a header on the
injected response, each requested twice: the second is a hit, identical.

- [ ] T005 [US1] Tests in `tests/unit/test_cached_responses.py` for Story 1's acceptance scenarios
  and edge cases (file and streaming responses not stored, background tasks only on the miss,
  a dependency's header on the injected response not replayed, `Link` on paginated calls not
  stored, `Content-Length` matches, pickle coder too, legacy entries still served, a 0.7.x-style
  reader recomputes an envelope; FR-011: envelopes with signing, encryption, compression, the size
  limit, tags and single flight)
- [ ] T006 [US1] In `fastapi_alcazaba/response_cache/decorator.py`: snapshot the injected
  response's headers before the endpoint runs (`_CachedCall._compute`), and compute the endpoint's
  added headers after
- [ ] T007 [US1] In `decorator.py`: `_encode` stores a returned in-memory `Response` as a
  `response` entry, and a value with endpoint headers as a `value` entry (`coder.encode` payload);
  bare values unchanged; responses without an in-memory body not stored
- [ ] T008 [US1] In `decorator.py`: `_serve` parses envelopes — `response` → rebuild a `Response`
  (status, headers in order, body, Content-Length); `value` → decode with the coder as today and
  add the stored headers to the injected response; unusable envelope → recompute
- [ ] T009 [US1] 304 path: answer with the stored `Cache-Control`, `Expires`, `Vary`,
  `Content-Location` (R8) in `decorator.py`

**Checkpoint**: Story 1 passes on its own.

## Phase 4: User Story 2 — The endpoint decides how clients and CDNs cache it (P2)

**Goal**: configured directives > the endpoint's own `Cache-Control` > today's; per-user entries
never `public`.

**Independent Test**: an endpoint setting `public, max-age=999`, one with
`cache_control="no-cache"`, one per-user with `public`: headers on miss and hit.

- [ ] T010 [US2] Tests in `tests/unit/test_cached_responses.py` for Story 2's acceptance scenarios,
  plus `cache_control` validation errors and a 304 carrying the stored `Cache-Control` (T009)
- [ ] T011 [US2] `cache_control` parameter on `cache()` and `_Endpoint` in `decorator.py`, validated
  when decorating (no line breaks, no `no-store`)
- [ ] T012 [US2] `_HeaderPolicy` in `decorator.py`: per target, configured directives (+ `max-age`
  if absent, + `stale-while-revalidate`), else the target's own header, else today's; `private`
  enforcement for per-user entries
- [ ] T013 [P] [US2] Typing test for `cache_control` in `tests/typing/test_public_api.py`

**Checkpoint**: Stories 1 and 2 pass.

## Phase 5: User Story 3 — What's stored is what the client receives (P3)

**Goal**: with a route response model, the entry holds the rendered output only.

**Independent Test**: `response_model=Public` over a dict with `password_hash`: the stored entry
lacks it; misses and hits return the same.

- [ ] T014 [US3] Tests in `tests/unit/test_cached_responses.py`: the dropped field isn't stored;
  include/exclude/by_alias/exclude_unset/exclude_none respected; a return annotation wider than
  the response model still hits; no route in scope → stored as today; a value that fails
  validation isn't stored
- [ ] T015 [US3] In `decorator.py`: when `request.scope["route"]` has a response model and the
  endpoint returned a value, render it (R7, adapters cached through `Coder.adapter`) and store a
  `rendered` entry; `_serve` returns its decoded JSON without validating it as the return type

**Checkpoint**: all stories pass.

## Phase 6: Polish & cross-cutting

- [ ] T016 [P] Live round trip of each entry kind in `tests/integration/test_backends_live.py`
- [ ] T017 [P] `docs/guide/cache.md`: "What a hit returns" and "Cache-Control" sections; the
  `cache_control` row in the `@cache()` table
- [ ] T018 [P] `CHANGELOG.md` under Unreleased: Added (whole responses, `cache_control`, rendered
  entries), Changed (the endpoint's `Cache-Control` now wins; returned `JSONResponse` replayed as a
  response; pickle no longer stores returned `Response`s)
- [ ] T019 `make lint`, unit + typing tests with coverage ≥ 90 %, `mkdocs build --strict`, the
  benchmarks (`pytest tests/benchmarks --benchmark-only`), and quickstart.md's checks

## Dependencies & Execution Order

- Setup (T001) → Foundational (T002–T004) → US1 (T005–T009) → US2 (T010–T013) → US3 (T014–T015) →
  Polish (T016–T019).
- US2 and US3 depend on the envelope (Phase 2); US2's per-target policy uses the stored headers
  from US1 (T008), so US1 comes first. US3 only needs Phase 2 and T007/T008's entry handling.
- Within a story: tests first, failing; then implementation.

## Parallel Opportunities

- T003 and T004 (different files) once T002 exists.
- T013 alongside T011–T012.
- T016, T017, T018 together after US3.

## Implementation Strategy

MVP = Phases 1–3 (Story 1): it alone fixes the lost headers and the non-JSON responses. Then
Story 2, then Story 3; each checkpoint leaves the suite green.
