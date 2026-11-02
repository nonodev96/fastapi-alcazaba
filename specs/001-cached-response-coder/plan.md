# Implementation Plan: Whole cached responses and the endpoint's own Cache-Control

**Branch**: `feature/cached-response-coder` | **Date**: 2026-11-02 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-cached-response-coder/spec.md`

## Summary

A hit must give back what the miss gave: status, body and the endpoint's own headers, for any
in-memory response, with the default coder; the endpoint's `Cache-Control` wins unless `@cache`
is told otherwise; and an entry holds only what the response model lets through. The decorator
stores a small versioned envelope (metadata line + raw bytes) whenever there is more than a bare
value; bare values keep today's format, so existing entries keep being served (research R1, R2).

## Technical Context

**Language/Version**: Python 3.11–3.14

**Primary Dependencies**: FastAPI ≥ 0.115, Starlette, Pydantic v2 (`TypeAdapter`); no new ones

**Storage**: the configured cache backend (memory, Redis, Valkey, memcached, DynamoDB, tiered)

**Testing**: pytest (`tests/unit`, `tests/typing`; live backends in `tests/integration`)

**Target Platform**: any ASGI server running FastAPI

**Project Type**: library

**Performance Goals**: no regression of the hit path in `tests/benchmarks` (gated by
`scripts/bench_gate.py`)

**Constraints**: entries of 0.7.x must keep working, and 0.7.x must survive entries of this
version during a rolling deploy (R2); no API removed

**Scale/Scope**: `fastapi_alcazaba/response_cache/` (decorator.py, a new entry module); docs
`docs/guide/cache.md`; tests

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
| --- | --- | --- |
| I. Secure by default | Never stores `Set-Cookie`, hop-by-hop, per-request headers written by dependencies (R3), or fields the response model drops (R7); per-user entries never `public` (R6). Returned `Response`s are never pickled any more (R1). Entries are still signed/encrypted as configured. | Pass |
| I. Fuzz targets for outside data | The envelope is read from the backend: a fuzz target for its parser is added (`fuzz/`), with its properties (never raises but a decode error, never yields a forbidden header). | Pass (task) |
| II. Tests | Every requirement gets unit tests; the envelope also runs against live backends in `tests/integration`; a test fails without each fix. | Pass (tasks) |
| III. Quality | ruff, mypy --strict, pylint, signatures; typing test for the new `cache_control` parameter. | Pass (tasks) |
| IV. Shared foundations | One format for every backend and coder; no backend-specific code. | Pass |
| V. Docs and changelog | `docs/guide/cache.md` sections; CHANGELOG under Unreleased, with the change of default (endpoint's `Cache-Control` wins) called out. | Pass (tasks) |
| VI. Dependencies, simplicity | No new dependency; one new parameter (`cache_control`). | Pass |

Re-check after design: unchanged; no violation to justify.

## Project Structure

### Documentation (this feature)

```text
specs/001-cached-response-coder/
├── spec.md
├── plan.md              # this file
├── research.md          # decisions R1–R9
├── data-model.md        # the stored entry and the Cache-Control policy
├── quickstart.md        # how to check it end to end
├── contracts/
│   └── cache-decorator.md  # the public behaviour of @cache after this change
├── checklists/
│   └── requirements.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
fastapi_alcazaba/response_cache/
├── decorator.py         # capture endpoint headers, choose envelope or bare value, Cache-Control
└── entries.py           # NEW: the envelope (encode, parse, storable headers, rebuild a Response)

fuzz/
└── entries_fuzzer.py    # NEW: parse arbitrary bytes as an entry

tests/
├── unit/test_cached_responses.py      # NEW: Stories 1–3, edge cases, legacy entries
├── unit/test_fuzz_targets.py          # + the new target
├── integration/test_backends_live.py  # + envelope round trip on live backends
└── typing/test_public_api.py          # + cache_control parameter

docs/guide/cache.md      # "What a hit returns" and "Cache-Control" sections
CHANGELOG.md             # Unreleased: Added / Changed
```

**Structure Decision**: one new module, `response_cache/entries.py`, holds the envelope so that
`decorator.py` (already ~860 lines) only decides what to store and applies headers; everything
else stays where it is.

## Changes to existing behaviour (for SC-004)

- `Cache-Control` set by the endpoint is kept instead of overwritten (FR-006). Tests asserting the
  overwrite, if any, change on purpose.
- A `JSONResponse` returned by an endpoint is now replayed as that response (status, headers, body)
  instead of its decoded body as a plain value.
- With pickle, a returned `Response` is stored as an envelope, not pickled.

Existing tests changed on purpose: one, `test_security.py`'s
`test_entries_stored_by_earlier_versions_are_recomputed`, whose simulation of 0.7.0 now also
stores the `Response` pickled, as 0.7.0 did (the new format drops its cookie anyway).

## Complexity Tracking

No constitution violations.
