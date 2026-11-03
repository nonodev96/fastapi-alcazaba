# Feature Specification: Step-up by factor

**Feature Branch**: `feature/stepup-by-factor` · **Created**: 2026-11-03 · **Status**: Done

**Input**: "Sensitive actions should be able to require a specific, recent authentication
method, for browser sessions and for API clients."

## Clarifications (no question asked; no backward compatibility kept)

- `require_recent(..., methods=...)` compares the session mark's method ("passkey", "totp") or a
  token's `amr` values (RFC 8176) with `methods`, literally.
- Tokens come in through a `claims` dependency that returns the claims dict; then `auth_time`,
  `amr` and `acr` decide, and a refusal is RFC 9470's 401 challenge.
- Refusals are problems (`urn:fastapi-alcazaba:problem:step-up`) with `max_age`, `methods` and
  `acr_values`, so a front end knows what to ask for.
- Audit: each check is an `auth.stepup` event (method, age, reason).

## Acceptance

Sessions: a method outside `methods`, an old mark, no mark → refused; tokens: no or old
`auth_time`, another `amr` or `acr` → the challenge; the audit records both outcomes.

## Requirements

- **FR-001** methods for sessions; **FR-002** tokens with auth_time/amr/acr and RFC 9470;
  **FR-003** problem details; **FR-004** audit. Tests in `tests/unit/test_stepup.py`; the
  two-factor guide.
