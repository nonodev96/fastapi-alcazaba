# Tasks: JWT signing keys of any type that rotate, and robust JWKS

**Input**: Design documents from `specs/004-jwt-key-rotation/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/jwt-keys.md,
quickstart.md

**Tests**: required by the constitution (principle II).

## Format: `[ID] [P?] [Story] Description`

## Phase 1: User Story 1 — Rotate keys of any type (P1) 🎯 MVP

- [X] T001 [US1] `JWTKey` in `fastapi_alcazaba/auth/strategies/jwt.py`: secret, private or public
  key; algorithm pinned; derived `kid` (RFC 7638 thumbprint, HMAC of a constant); `until`
- [X] T002 [US1] `JWTStrategy(keys=...)`: the first signs (with its `kid`), checks (unique `kid`s,
  first without `until`, no `algorithms` with `keys`); `key=` and `signing_key=` become keys
  (R6), `signing_key=` deprecated
- [X] T003 [US1] Verification by `kid`, the token's `alg` among its key's, retired keys refused,
  keys without a given `kid` tried in turn (R3)
- [X] T004 [US1] Unit tests in `tests/unit/test_jwt_keys.py`: every key type, mixed, rotation,
  retirement, `kid`s, algorithm confusion, old tokens without `kid`, setup errors

## Phase 2: User Story 2 — Robust JWKS (P1)

- [X] T005 [US2] The JWKS fetcher: httpx2 through `ResilientTransport`, HTTPS URL, lifespan,
  refetch rate, one fetch at a time, stale keys, usable keys only, `JWKSUnavailable` → 503;
  `aclose()`; httpx2 in the extra
- [X] T006 [US2] Metric `auth.jwks` (declared, documented)
- [X] T007 [US2] Tests with `httpx.MockTransport`: lifespan, unknown `kid` rate, rotation at the
  provider, concurrent refetches, timeouts and breaker, stale keys, 503, bad JWKS, `oct` and `enc`
  keys, `alg` mismatch; existing JWKS tests moved off `PyJWKClient`

## Phase 3: User Story 3 — Publish public keys (P3)

- [X] T008 [US3] `jwks()` and its tests
- [X] T008b [US2] Property test of the JWKS parser with hypothesis (research R10)

## Phase 4: Polish

- [X] T009 Typing test of the public API (`JWTKey`, `keys=`, `jwks()`)
- [X] T010 [P] JWT guide: rotation, retirement, mixing, publishing, JWKS behaviour; reference;
  observability table; README if a row applies
- [X] T011 CHANGELOG (Added, Changed, Deprecated)
- [X] T012 `make lint`, unit tests with coverage, `mkdocs build --strict`
