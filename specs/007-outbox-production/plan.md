# Implementation Plan: An outbox that runs for months

**Branch**: `feature/outbox-production` | **Date**: 2026-11-03 | **Spec**: [spec.md](spec.md)

## Summary

Purging (R1) and recovery (R2) in `SQLAlchemyOutbox`, run by `OutboxRelay`; trace context in a
column, spans and counters (R3, R4); indexes for the relay's queries (R5); async and PostgreSQL
tests (R6).

## Technical Context

Python 3.11–3.14 · SQLAlchemy 2 (the `sqlalchemy` extra) · tests: SQLite (sync, aiosqlite),
PostgreSQL (asyncpg) · `background/outbox.py`, `telemetry/{tracing,metrics}.py`, Makefile, CI.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Secure by default | No payload in `given_up()`; spans carry ids and topics only | Pass |
| II. Tests | Sync, async, PostgreSQL, `run()` | Pass (tasks) |
| III. Quality | ruff, mypy, pylint | Pass (tasks) |
| IV. Shared foundations | Tracer, Metrics, traceparent of the task queue | Pass |
| V. Docs and changelog | background.md#outbox, CHANGELOG (migration) | Pass (tasks) |
| VI. Simplicity | Through the app's sessions; no ORM of ours | Pass |

## Changes to existing behaviour

- `outbox_table` gains `traceparent` and different indexes: a migration (or `traceparent=False`).
- The relay purges sent messages after 7 days by default.
