# Tasks: Every backend can do what the tools need

**Input**: Design documents from `specs/002-backend-parity/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/backends.md,
quickstart.md

**Tests**: required by the constitution (principle II).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)

## Phase 1: Setup

- [X] T001 List the tests that assert what memcached and DynamoDB can't do
  (`tests/unit/test_backends.py`, `tests/integration/*_live.py`) in plan.md's "Changes to existing
  behaviour"

## Phase 2: Foundational (blocking US2 and US3)

- [X] T002 Create `fastapi_alcazaba/backends/_generations.py`: `namespaces(key)`, `MAX_DEPTH`,
  `check_depth(namespace)`, `namespace_generation`/`tag_generation` keys, `new_token()`, `Stamp`,
  `encode_stamp`, `parse_stamp` (None without the magic, `ValueError` when malformed), `alive()`
- [X] T003 [P] Unit tests for it in `tests/unit/test_backends.py` (`TestGenerations`): namespaces of
  keys with and without prefix, state keys, depth limit; stamp round trip; malformed stamps;
  `alive` with equal, changed, absent and "" tokens
- [X] T004 [P] Fuzz target `fuzz/stamps_fuzzer.py` (any bytes: None, a stamp that encodes back the
  same, or `ValueError`) and its known cases in `tests/unit/test_fuzz_targets.py`

**Checkpoint**: generations exist and are tested; no backend uses them yet.

## Phase 3: User Story 1 — Locks on memcached (P1) 🎯 MVP

**Goal**: memcached holds locks like Redis.

**Independent Test**: contenders, wrong tokens and expiry against a fake and a real memcached.

- [X] T005 [US1] Unit tests in `tests/unit/test_backends.py`: acquire once, release and extend only
  by the holder, expiry `ceil(ttl) + 1` (and as a Unix time past 30 days), release writes an expired
  value, `cas` lost to another writer; the fake gains `gets`, `cas`, `multi_get` and expiry
- [X] T006 [US1] `MemcachedBackend.acquire_lock`/`release_lock`/`extend_lock`, `supports_locks =
  True`, in `fastapi_alcazaba/backends/memcached.py`
- [X] T007 [US1] Messages of the tools that refuse a backend without locks (`auth/totp.py`,
  `rate_limit/concurrency.py`, `background/locks.py`) say every shipped backend can; their tests

## Phase 4: User Story 2 — Clearing a namespace or a tag (P2)

**Goal**: `clear(namespace=...)` and tags on memcached and DynamoDB, through generations.

**Independent Test**: two namespaces and two tags; clear one of each; check what's served.

- [X] T008 [US2] Unit tests (memcached fake and DynamoDB fake): namespace clear hides only its
  entries, `clear()` the prefix, state survives, a clear between reading generations and writing
  the entry, tags, eviction of a generation (memcached), entries of 0.7, too deep a namespace,
  corrupt stamps; the DynamoDB fake gains `batch_get_item` (with unprocessed keys) and the
  `tag_generations` update
- [X] T009 [US2] memcached: stamped `set`/`get_with_ttl` for non-state keys (generations created
  with `add` when missing), `clear(namespace)`, `add_tags` (`gets` + `cas`), `invalidate_tags`,
  `supports_tags = True`
- [X] T010 [US2] DynamoDB: generations read with `BatchGetItem` beside the entry, written as
  `generations`; `clear(namespace)`; `add_tags` (`UpdateItem` of `tag_generations`, only if the
  entry exists); `invalidate_tags`; `supports_tags = True`
- [X] T011 [US2] `base.py` docstrings: what `clear` and `invalidate_tags` return on backends that
  can't count

## Phase 5: User Story 3 — Remaining lifetime on memcached (P3)

- [X] T012 [US3] Unit tests: `get_with_ttl` gives the remaining lifetime; no expiry gives -1;
  `early_refresh` refreshes on memcached (through the decorator)

(The code is T009's stamp: its expiry.)

## Phase 6: Polish & Cross-Cutting

- [X] T013 Live tests in `tests/integration/test_backends_live.py`: TTL on every backend, namespace
  clear and tags without skips (counts only where the backend counts), locks with memcached's whole
  seconds, a real `distributed_lock` run with `single_flight=False` from two caches, entries of 0.7
  on memcached; `test_user_sessions` without its memcached branch
- [X] T014 [P] `tests/integration/test_coordination_live.py`: the concurrency limiter and the
  leader on memcached (no skip); a live `FastAPITOTP` test on every backend
- [X] T015 [P] Docs: `docs/guide/backends.md` (descriptions and table), `background.md`,
  `limits.md`, `two-factor.md`; CHANGELOG under Unreleased (Added; Changed: clears return 0 on
  memcached and DynamoDB, memcached's cached responses of 0.7 recomputed once)
- [X] T016 Run `make lint`, the unit suite with coverage, the integration suite on live backends,
  and `mkdocs build --strict`

## Dependencies & Execution Order

- T002 blocks T008–T012; T005–T007 (US1) only need T001.
- T013–T016 after the stories.
