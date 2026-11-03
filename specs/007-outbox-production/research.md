# Research: An outbox that runs for months

Findings from reading `fastapi_alcazaba/background/outbox.py`, its tests, `telemetry/tracing.py`,
the task queue's tracing, the Makefile and `.github/workflows/ci.yml`, on 2026-11-03.

## R1. Purging

- **Decision**: `SQLAlchemyOutbox.purge(older_than, *, batch=1000) -> int` deletes rows with
  `sent < now - older_than` and `failed IS NULL`, `batch` ids at a time, each batch its own
  transaction, until a batch comes back short. `OutboxRelay(retention=7 days, purge_interval=3600,
  purge_batch=1000)`: `run()` purges when `purge_interval` has passed since the last purge (and at
  start); `retention=None` never purges.
- **Rationale**: short transactions don't hold locks the writers need; given-up messages are what
  an operator must look at, so they stay until requeued or discarded.

## R2. Messages given up on

- **Decision**: `given_up(limit=100) -> list[GivenUpMessage]` (id, topic, attempts, created,
  failed, error; no payload: it may hold personal data, and the operator has the database),
  newest first. `requeue(ids=None) -> int`: `sent`, `failed`, `error` cleared, `attempts` 0,
  `next_try` now, only where `failed IS NOT NULL`. `discard(ids) -> int`: delete, same condition.

## R3. Trace context

- **Decision**: `add()` stores `traceparent()` (the current span's, W3C, 55 characters) in the
  nullable column `traceparent`; `OutboxMessage.traceparent`. The relay opens
  `fastapi_alcazaba.outbox <topic>`, a CONSUMER span that's the root of a trace of its own linked
  to it, around `deliver`: a task enqueued there carries the delivery's context. `outbox_table(...,
  traceparent=False)` leaves the column out; the outbox writes and reads it only when the table has
  it.
- **Rationale**: as task runs (a delivery may come hours later and many times); the link keeps the
  path from the request.

## R4. Metrics and the providers

- **Decision**: counter `outbox` (delivered, retried, given_up; by outbox and topic) and
  `outbox.purged` (rows, by outbox). Metrics and tracer: FastAPICache's when it's set up; else
  `Metrics()` and `Tracer()` on the global providers.
- **Topics** are names the app writes (`order.placed`): low cardinality.

## R5. Indexes

- **Decision**: `Index("ix_<table>_due", sent, next_try)` and `Index("ix_<table>_failed",
  failed)`, instead of single indexes on `next_try` and `sent`. The relay's `sent IS NULL AND
  next_try <= now`, the backlog's `sent IS NULL`, the purge's `sent < cutoff` all start with
  `sent`; on `next_try` alone, every sent row (whose `next_try` stays in the past) matched the
  relay's range. The integration test asks PostgreSQL's planner (EXPLAIN) that the due index serves
  the relay's query.

## R6. Tests and services

- **Decision**: dev group gains `aiosqlite`, `asyncpg`, `greenlet` (SQLAlchemy's async needs it).
  `make services-up` and CI start `postgres:17`; `POSTGRES_URL`
  (`postgresql+asyncpg://postgres:postgres@127.0.0.1:15432/postgres` locally) enables
  `tests/integration/test_outbox_live.py`, each test in a table of its own.
