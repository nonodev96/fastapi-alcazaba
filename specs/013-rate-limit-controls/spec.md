# Feature Specification: Rate limit controls

**Feature Branch**: `feature/rate-limit-controls` · **Created**: 2026-11-03 · **Status**: Done

**Input**: "Apps need to adjust rate limits at run time, not only declare them."

## Clarifications (no question asked; no backward compatibility kept)

- `times` is an int or a function of the request; the counters' key says "dynamic" instead of
  the number, so a client whose limit changes keeps what it spent.
- `exempt`: identities (as `identifier` returns them) or a predicate; exempt requests are counted
  as `exempt` and logged once per identity and process; they get no RateLimit headers.
- `reset()` deletes the client's counters (both windows) in the shared backend and this
  process's block; other processes' blocks lapse when the budget would have refilled.
- `status()` reads both counters without spending (`RateLimitStatus`).
- Without a request: `identity=` and `cache=`, for named limiters only.

## Acceptance

Unit tests (`tests/unit/test_rate_limit_controls.py`), a live test on every backend, the limits
guide with the login example resetting after a right password.
