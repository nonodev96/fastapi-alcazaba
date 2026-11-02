# Implementation Plan: Refresh tokens that rotate and detect reuse

**Branch**: `feature/refresh-tokens` | **Date**: 2026-11-03 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/003-refresh-tokens/spec.md`

## Summary

A `RefreshTokens` component next to `TokenBlocklist`: it issues pairs through a `JWTStrategy`,
keeps one record per login (family) in the cache's backend, and exchanges refresh tokens under the
family's lock. Tokens are HMACs of their family and generation, so any generation is verifiable
and none is stored (research R1); the previous generation is honoured for a grace period (R3);
reuse revokes the family, whose access tokens the blocklist refuses through their `sid` (R4).

## Technical Context

**Language/Version**: Python 3.11–3.14

**Primary Dependencies**: PyJWT (the `auth-strategy-jwt` extra); no new ones

**Storage**: the cache's backend (state keys)

**Testing**: pytest (unit with InMemoryBackend; live on every backend)

**Project Type**: library

**Constraints**: no change for apps that don't use it; `TokenBlocklist` and `JWTStrategy` gain the
session id without changing what they did

**Scale/Scope**: `fastapi_alcazaba/auth/refresh.py` (new), `auth/tokens.py`,
`auth/strategies/jwt.py`, `auth/__init__.py`; docs; the JWT example and its animation; tests

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Secure by default | Nothing stored makes a token (R1); reuse revokes; forgeries don't (R1); fails closed; a strategy without blocklist or a lockless backend refused (FR-009). Refresh tokens come from clients: fuzz target for the parser. | Pass |
| II. Tests | Unit tests per requirement; live tests on every backend (rotation, concurrency, reuse). | Pass (tasks) |
| III. Quality | ruff, mypy --strict, pylint, signatures; typing test of the public API. | Pass (tasks) |
| IV. Shared foundations | The cache's backend, guard, locks, metrics and audit trail; `TokenBlocklist` for access tokens. | Pass |
| V. Docs and changelog | JWT guide section, the example, README row, CHANGELOG. | Pass (tasks) |
| VI. Simplicity | One class, one exception, one dataclass; no option the spec doesn't need. | Pass |

## Project Structure

```text
specs/003-refresh-tokens/   spec, research, plan, data-model, contracts/refresh-tokens.md, quickstart, checklists, tasks

fastapi_alcazaba/auth/
├── refresh.py           # NEW: RefreshTokens, TokenPair, RefreshRefused
├── tokens.py            # TokenBlocklist.revoke_session, is_revoked(session=)
├── strategies/jwt.py    # checks the token's sid against the blocklist
└── __init__.py          # exports

fuzz/refresh_fuzzer.py   # NEW
examples/auth_strategies/jwt_tokens.py   # refresh, logout
docs/guide/authentication/jwt.md, docs/examples/auth_strategies/jwt.md, scripts/docs_animations.py
tests/unit/test_refresh_tokens.py (NEW), test_tokens*.py, test_example_auth_jwt.py,
tests/typing/test_public_api.py, tests/integration/test_backends_live.py
```

## Changes to existing behaviour

- `JWTStrategy` with a blocklist checks the `sid` of tokens that have one: one more read per
  request for those tokens only.

## Complexity Tracking

No constitution violations.
