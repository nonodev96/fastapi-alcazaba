# Implementation Plan: Every backend can do what the tools need

**Branch**: `feature/backend-parity` | **Date**: 2026-11-02 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-backend-parity/spec.md`

## Summary

memcached gets locks (`add`, `gets` + `cas`), and memcached and DynamoDB get namespace clears and
tag invalidation through generations: a random token per namespace or tag, recorded in each
entry's stamp, replaced by each clear (research R1–R4). memcached entries carry their expiry in the
same stamp, so hits know their remaining lifetime. Redis, Valkey, GLIDE and InMemory don't change.

## Technical Context

**Language/Version**: Python 3.11–3.14

**Primary Dependencies**: aiomcache 0.8, aiobotocore; no new ones

**Storage**: memcached, DynamoDB

**Testing**: pytest (`tests/unit` with fakes; `tests/integration` against real memcached and
DynamoDB Local)

**Target Platform**: any process using the backends

**Project Type**: library

**Performance Goals**: one round trip per untagged hit on memcached and DynamoDB (SC-004)

**Constraints**: no behaviour change on Redis/Valkey/GLIDE/InMemory; state keys stored as today on
memcached (locks, counters, sessions keep working across a rolling upgrade)

**Scale/Scope**: `fastapi_alcazaba/backends/` (memcached.py, dynamodb.py, a new
`_generations.py`, base.py docstrings); messages in totp.py, concurrency.py, locks.py; docs; tests

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
| --- | --- | --- |
| I. Secure by default | Locks only released or extended by their holder (R1); a clear is never undone by an eviction (R4); entries computed across a clear aren't served (R2); state never hidden by a clear (FR-007). Stamps are read from the backend: fuzz target. | Pass |
| II. Tests | Unit tests with fakes for every requirement; live tests for locks, clears, tags and TTL on every backend, plus TOTP, the concurrency limiter, idempotency and `distributed_lock` on memcached. | Pass (tasks) |
| III. Quality | ruff, mypy --strict, pylint, signatures. | Pass (tasks) |
| IV. Shared foundations | One generations module for both backends; the tools keep using `supports_*`. | Pass |
| V. Docs and changelog | backends.md table, the pages saying memcached can't lock, CHANGELOG (Added, Changed: 0 returned by clears, memcached entries of 0.7 recomputed once). | Pass (tasks) |
| VI. Simplicity | No new option or dependency. | Pass |

Re-check after design: unchanged.

## Project Structure

### Documentation (this feature)

```text
specs/002-backend-parity/
├── spec.md
├── plan.md              # this file
├── research.md          # decisions R1–R8
├── data-model.md        # generations and the stamp
├── quickstart.md
├── contracts/
│   └── backends.md      # what each backend guarantees after this change
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
fastapi_alcazaba/backends/
├── _generations.py      # NEW: namespaces of a key, generation keys, the stamp (encode, parse), alive()
├── memcached.py         # locks, stamps (TTL, generations), clear(namespace), tags
├── dynamodb.py          # generations as attributes, BatchGetItem, clear(namespace), tags
└── base.py              # docstrings: what clear and invalidate_tags return here

fuzz/stamps_fuzzer.py    # NEW
tests/unit/test_backends.py, tests/unit/test_fuzz_targets.py
tests/integration/test_backends_live.py, test_coordination_live.py
docs/guide/backends.md, background.md, limits.md, two-factor.md; CHANGELOG.md
```

**Structure Decision**: the generation logic both backends share goes in one private module; each
backend keeps its own I/O.

## Changes to existing behaviour

- memcached: `supports_locks` and `supports_tags` become true; `get_with_ttl` gives the remaining
  lifetime instead of -1; `clear(namespace=...)` works (returns 0) instead of raising.
- DynamoDB: `supports_tags` becomes true; `clear(namespace=...)` works (returns 0).
- memcached: cached responses stored by 0.7 are misses once.

Existing tests changed on purpose: `tests/unit/test_backends.py` (`test_set_get_and_unknown_ttl`,
`test_clear`, `test_what_it_cant_do_says_so` for memcached; DynamoDB's namespace-clear refusal) and
the live tests that skipped memcached and DynamoDB (`_no_namespace_clear`, `supports_locks` skips,
the memcached branch of `test_user_sessions`).

## Complexity Tracking

No constitution violations.
