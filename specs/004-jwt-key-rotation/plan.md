# Implementation Plan: JWT signing keys of any type that rotate, and robust JWKS

**Branch**: `feature/jwt-key-rotation` | **Date**: 2026-11-03 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-jwt-key-rotation/spec.md`

## Summary

`JWTKey` pins a key to one algorithm and names it (R1, R2); `JWTStrategy(keys=[...])` signs with
the first and picks the key of each token by its `kid` (R3), honouring retirement dates (R4). The
remote JWKS moves from `PyJWKClient` to httpx2 through `ResilientTransport`, with a lock for one
fetch at a time, a refetch rate and stale keys (R7).

## Technical Context

**Language/Version**: Python 3.11–3.14

**Primary Dependencies**: PyJWT[crypto]; httpx2 joins the `auth-strategy-jwt` extra (JWKS only)

**Storage**: none (keys in memory)

**Testing**: pytest; JWKS with httpx's `MockTransport`

**Project Type**: library

**Constraints**: the old constructor arguments keep working (FR-008); tokens issued before (no
`kid`) keep verifying (FR-004)

**Scale/Scope**: `fastapi_alcazaba/auth/strategies/jwt.py`, `pyproject.toml`, metrics, docs, tests

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Secure by default | Algorithm pinned per key; no HMAC from a JWKS; HTTPS JWKS; fail closed (503) | Pass |
| II. Tests | Unit tests per requirement and key type; fake JWKS server | Pass (tasks) |
| III. Quality | ruff, mypy --strict, pylint, signatures; typing test | Pass (tasks) |
| IV. Shared foundations | `ResilientTransport`, `_secure`, metrics | Pass |
| V. Docs and changelog | JWT guide, reference, CHANGELOG | Pass (tasks) |
| VI. Simplicity | One class (`JWTKey`), one exception (`JWKSUnavailable`) | Pass |

## Project Structure

```text
specs/004-jwt-key-rotation/   spec, research, plan, data-model, contracts/jwt-keys.md, quickstart, checklists, tasks

fastapi_alcazaba/auth/strategies/jwt.py   JWTKey, JWKSUnavailable, keys=, jwks(), aclose(), the JWKS fetcher
fastapi_alcazaba/telemetry/metrics.py     auth.jwks
pyproject.toml                            httpx2 in auth-strategy-jwt
docs/guide/authentication/jwt.md, docs/guide/observability.md, docs/reference/authentication.md
tests/unit/test_jwt_keys.py (NEW), tests/unit/test_auth_strategies.py, tests/typing/test_public_api.py
```

## Changes to existing behaviour

- Issued tokens carry a `kid` header.
- `jwks_url` must be HTTPS (or http://localhost); HMAC `algorithms` with it are refused.
- A provider that can't be reached gives a 503 instead of a 401; an unknown `kid` is still a 401.
- `signing_key=` warns `DeprecationWarning`.

## Complexity Tracking

No constitution violations.
