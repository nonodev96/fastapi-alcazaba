# Feature Specification: An outbox that runs for months

**Feature Branch**: `feature/outbox-production`

**Created**: 2026-11-03

**Status**: Draft

**Input**: User description: "The outbox should be safe to run for months: its table must not grow
without bound, failed messages must be recoverable, and its work must be observable end to end."

## Clarifications

### Session 2026-11-03

No question was put to the maintainer, who asked for the feature to be built and merged. The
choices a clarification would have asked about are recorded, with their reasons, in research.md:

- The relay purges messages sent more than `retention` ago (7 days), every `purge_interval` (an
  hour), in batches; messages given up on are kept until someone requeues or discards them (R1).
- The trace context is a new nullable column, `traceparent`; `outbox_table(...,
  traceparent=False)` keeps a table without it. Each delivery is the root of a trace linked to the
  request that wrote the message, as task runs are (R3).
- The relay's metrics and traces are FastAPICache's when it's set up, the global providers
  otherwise (R4).
- The indexes become one on (sent, next_try), what the relay, the backlog and the purge read, and
  one on `failed`; the table's definition changes, which a migration applies (R5).
- PostgreSQL joins the integration services, with asyncpg; SQLite through aiosqlite covers the
  async path in the unit tests (R6).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The table doesn't grow without bound (Priority: P1)

**Acceptance Scenarios**:

1. **Given** messages sent 8 days ago and 1 day ago, **When** the relay purges (retention 7 days),
   **Then** the first are deleted, the second kept, and those not sent or given up on kept.
2. **Given** more messages than a batch, **Then** they're deleted in several short transactions.
3. **Given** `retention=None`, **Then** nothing is purged.

---

### User Story 2 - Recover messages given up on (Priority: P1)

**Acceptance Scenarios**:

1. **Given** messages given up on, **When** `given_up()` is called, **Then** they're listed (id,
   topic, attempts, when written and given up, last error), never their payload.
2. **When** `requeue(ids)` (or all), **Then** they're due now with their attempts reset, and the
   relay delivers them.
3. **When** `discard(ids)`, **Then** they're deleted; a message that wasn't given up on is
   neither requeued nor discarded.

---

### User Story 3 - Observable end to end (Priority: P2)

**Acceptance Scenarios**:

1. **Given** a request with a span writes a message, **When** the relay delivers it, **Then** the
   delivery's span is linked to the request's, and a task the delivery enqueues links to the
   delivery's.
2. **Then** the counter `outbox` counts delivered, retried and given_up, by outbox and topic;
   `outbox.purged` counts the rows purged.
3. **Given** a delivery that fails, **Then** its span records the error.

---

### User Story 4 - Tested where it runs (Priority: P2)

**Acceptance Scenarios**:

1. **Given** an `async_sessionmaker` (aiosqlite, asyncpg), **Then** adding, relaying, purging and
   requeuing work as with a sync one.
2. **Given** `relay.run()`, **Then** it delivers, waits when there's nothing, purges on time, keeps
   going when the database fails, and stops when cancelled.

### Edge Cases

- A table without `traceparent` (`traceparent=False`): messages carry no trace context, nothing
  fails.
- A stored `traceparent` that isn't one: no link, the delivery goes on.
- A purge that fails (the database down): logged, the relay goes on.

## Requirements *(mandatory)*

- **FR-001**: The relay MUST purge sent messages older than `retention`, every `purge_interval`,
  in batches of `purge_batch`, never those not sent or given up on; `SQLAlchemyOutbox.purge()`
  does it on demand.
- **FR-002**: `given_up()`, `requeue()` and `discard()` MUST list, requeue and delete messages
  given up on, and only those.
- **FR-003**: Messages MUST carry the trace context of where they were written; deliveries MUST be
  spans linked to it, failures recorded; counters for delivered, retried, given_up and purged.
- **FR-004**: The async session path and `run()` MUST be tested, against PostgreSQL in the
  integration suite.
- **FR-005**: The relay's queries MUST be served by the table's indexes.

## Success Criteria *(mandatory)*

- **SC-001**: Unit tests on SQLite (sync and async), integration tests on PostgreSQL.
- **SC-002**: The outbox section of background.md covers purging, recovery, tracing and the
  migration; the CHANGELOG lists the table's changes.

## Assumptions

- The table is the app's, in its migrations: the CHANGELOG says what to migrate.

## Out of Scope

- Ordering guarantees across relays (one relay, under `FastAPILeader`, as before).
- An ORM layer of our own.
