# Feature Specification: Serving stale entries

**Feature Branch**: `feature/stale-serving` · **Created**: 2026-11-03 · **Status**: Done

**Input**: "The response cache should keep serving when fresh data is slow or unavailable."

## Clarifications (no question asked; no backward compatibility kept)

- Entries are stored for `expire` plus the longest stale window; a read tells a fresh entry
  from a stale one and its age past `expire` (R1).
- `stale_while_revalidate=N`: a stale entry up to N seconds past expiry is served at once, and
  refreshed after the response, once across processes (a try-once lock), once per process. In
  an endpoint, the refresh is one of the response's background tasks: it runs after the
  response is sent and before the request's dependencies are closed (R2).
- `stale_if_error=N`: up to N seconds past expiry, a stale entry is served when computing a
  fresh one raises, or takes longer than `refresh_timeout` (R3).
- `Cache-Status`: `hit; ttl=-<age>; detail=stale-while-revalidate` / `stale-if-error`; lookups
  are counted as `stale` and `stale_if_error`.
- Per endpoint; signed and encrypted entries, compression, single flight, the distributed lock,
  tags and generations as for any entry.

## Acceptance

Unit tests with a fake clock (InMemoryBackend's), a live test on every backend, the cache guide.

## Research

- R1: logical ttl = backend ttl − extra; an unknown ttl (≤ 0) counts as fresh.
- R2: an asyncio task outside routes; in routes, Starlette's background tasks of the response:
  the endpoint's arguments (a DB session of a dependency) are still open. The refresh stores,
  counts the store, and never touches the response already sent.
- R3: the stale fallback covers any Exception of the computation, including TimeoutError of
  `refresh_timeout`; not BaseException (cancellation).
