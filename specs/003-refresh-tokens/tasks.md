# Tasks: Refresh tokens that rotate and detect reuse

**Input**: Design documents from `specs/003-refresh-tokens/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/refresh-tokens.md,
quickstart.md

**Tests**: required by the constitution (principle II).

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Foundational

- [ ] T001 `TokenBlocklist.revoke_session(session, *, ttl)` and `is_revoked(..., session=)` in
  `fastapi_alcazaba/auth/tokens.py`; unit tests
- [ ] T002 `JWTStrategy.authenticate` passes the token's `sid` to the blocklist
  (`auth/strategies/jwt.py`); unit test: a token whose session was revoked is refused, others not

## Phase 2: User Story 1 — Stay logged in (P1) 🎯 MVP

- [ ] T003 [US1] `fastapi_alcazaba/auth/refresh.py`: `TokenPair`, `RefreshRefused`,
  `RefreshTokens.__init__` (checks of FR-009), `issue`, `exchange` for the current generation
  (lock, record, rotation, idle and absolute expiry), token format and parser
- [ ] T004 [US1] Unit tests in `tests/unit/test_refresh_tokens.py`: pairs, claims and `sid` kept,
  rotation, lifetimes, malformed tokens, secrets rotated, setup refusals
- [ ] T005 [P] [US1] Fuzz target `fuzz/refresh_fuzzer.py` (any string: a parsed token that formats
  back the same, or None) and its known cases

## Phase 3: User Story 2 — Theft noticed (P1)

- [ ] T006 [US2] Reuse of an older generation or of the previous one after `grace` revokes the
  family (record deleted, session revoked in the blocklist), warning log; forged MACs refused
  without revoking; client binding
- [ ] T007 [US2] Tests: reuse after an exchange, the new tokens refused afterwards (refresh and
  access through `JWTStrategy`), forged token with a real family id, wrong client

## Phase 4: User Story 3 — Concurrent refreshes (P2)

- [ ] T008 [US3] Grace: previous generation within `grace` gets the current refresh token and a
  new access token
- [ ] T009 [US3] Tests: five concurrent exchanges get the same refresh token; grace boundary

## Phase 5: User Story 4 — Logging out (P2)

- [ ] T010 [US4] `revoke(refresh_token)`, `revoke_session(sid)`, `revoke_subject(subject)` (cutoff
  + blocklist)
- [ ] T011 [US4] Tests for each, and a login right after `revoke_subject`

## Phase 6: Polish & Cross-Cutting

- [ ] T012 Metrics, audit events (`auth.refresh.*`) and their tests; backend failures fail closed
- [ ] T013 Exports in `fastapi_alcazaba/auth/__init__.py`; `tests/typing/test_public_api.py`
- [ ] T014 Live tests in `tests/integration/test_backends_live.py`: rotation, five concurrent
  exchanges, reuse, on every backend
- [ ] T015 [P] The JWT example (`/token` gives a pair, `/token/refresh`, logout revokes the family),
  its tests, its page and animation (`scripts/docs_animations.py`)
- [ ] T016 [P] Docs: "Refresh tokens" in `docs/guide/authentication/jwt.md`, `TokenBlocklist`'s
  sessions in `docs/guide/auth.md`, README row, CHANGELOG
- [ ] T017 `make lint`, unit with coverage, integration on live backends, `mkdocs build --strict`
