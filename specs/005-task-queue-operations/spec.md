# Feature Specification: A task queue operators can keep healthy

**Feature Branch**: `feature/task-queue-operations`

**Created**: 2026-11-03

**Status**: Draft

**Input**: User description: "Operators need to keep the task queue healthy without reading the
backend by hand: tasks that hang must stop, duplicates must not run twice, and a task must be
inspectable and cancellable."

## Clarifications

### Session 2026-11-03

No question was put to the maintainer, who asked for the feature to be built and merged. The
choices a clarification would have asked about are recorded, with their reasons, in research.md:

- A task past its `timeout` is cancelled and counts as a failure that raised `TimeoutError`: it's
  retried or goes to the dead letters like any other. A sync task's thread can't be stopped: its
  slot is freed, the thread runs on (R1).
- `unique_key` holds while the task is waiting, running or waiting for a retry; it's released when
  the task succeeds, goes to the dead letters or is cancelled (R2).
- `give_up_on` wins over `retry_on`; without `retry_on` every exception is retried (R3).
- Every task has a status record, kept `status_ttl` (one day) after it ends (R4).
- Only a task that hasn't started (waiting, delayed, or waiting for a retry) can be cancelled; a
  running one can't (R5).
- The command line points at a queue (`module:attribute`), or at a function that sets up the cache
  and returns it (R6).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A hung task doesn't block the queue (Priority: P1)

A task calls a service that never answers. Today it runs forever: its heartbeats keep it from
being taken over, and it holds one of the worker's `concurrency` slots. A few of those and the
queue stops.

**Acceptance Scenarios**:

1. **Given** `@queue.task(timeout=30)`, **When** a run takes longer, **Then** it's cancelled,
   retried (or sent to the dead letters with `TimeoutError`), and its slot runs other tasks.
2. **Given** no `timeout`, **Then** nothing changes.

---

### User Story 2 - A task enqueued twice runs once (Priority: P1)

The outbox relay delivers each message at least once; a client retries a request. Both enqueue
the same work twice.

**Acceptance Scenarios**:

1. **Given** `enqueue(task, ..., unique_key="invoice-42")`, **When** it's enqueued again while
   the first waits, runs or waits for a retry, **Then** the second returns the first's id and adds
   nothing.
2. **Given** the first succeeded, went to the dead letters or was cancelled, **When** it's
   enqueued again with that key, **Then** it's a new task.
3. **Given** many processes enqueue with one key at once, **Then** exactly one task is added.

---

### User Story 3 - Errors that retrying won't fix (Priority: P2)

**Acceptance Scenarios**:

1. **Given** `give_up_on=(ValueError,)`, **When** the task raises one, **Then** it goes to the
   dead letters at once, whatever its retries left.
2. **Given** `retry_on=(ConnectionError,)`, **When** it raises anything else, **Then** it goes to
   the dead letters at once; a `ConnectionError` is retried.

---

### User Story 4 - Look a task up, cancel it (Priority: P2)

Support asks what happened to the e-mail of an order; an operator wants to stop a task that was
enqueued by mistake.

**Acceptance Scenarios**:

1. **Given** a task id, **When** `status(id)` is called, **Then** it gives the state (pending,
   running, retrying, succeeded, dead, cancelled), the task's name, its attempts, its last error
   type, why it's dead, and when it was enqueued and last changed; never its arguments.
2. **Given** a pending, delayed or retrying task, **When** `cancel(id)` is called, **Then** it
   returns True and the task never runs; a running or finished one returns False.
3. **Given** an unknown id, or one whose status expired, **Then** `status` is None.

---

### User Story 5 - Operate from a shell (Priority: P3)

**Acceptance Scenarios**:

1. **Given** `fastapi-alcazaba tasks myapp.worker:queue dead-letters`, **Then** the dead letters
   are listed; `requeue`, `discard`, `pending`, `status` and `cancel` act as the methods do;
   `queues` lists every queue of a module with its pending tasks and dead letters.
2. **Given** `--json`, **Then** the output is JSON.

### Edge Cases

- A task cancelled while a worker is about to start it: either it runs (cancel returns False) or
  it doesn't, never both.
- A task enqueued by 0.7 (no status record): `status` is None until a worker starts it.
- A copy of a sealed task written back (replayed) doesn't change the real task's status.
- A refused task that can't be read (malformed, tampered) has no status to change.
- `unique_key` and `delay` together: the key holds while it waits for its time.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `task(timeout=...)` MUST cancel a run past it and handle it as a failure raising
  `TimeoutError`, freeing its slot.
- **FR-002**: `enqueue(..., unique_key=...)` MUST add at most one task per key while one with that
  key is waiting, running or waiting for a retry, atomically, and return the existing task's id
  otherwise.
- **FR-003**: `task(retry_on=..., give_up_on=...)` MUST send a task whose exception isn't retried
  to the dead letters at once.
- **FR-004**: `status(id)` MUST give a task's state without its arguments; `cancel(id)` MUST stop
  a task that hasn't started, atomically with respect to workers starting it.
- **FR-005**: A command line (`fastapi-alcazaba tasks`) MUST list queues, pending tasks and dead
  letters, requeue and discard dead letters, and look up and cancel tasks, without new
  dependencies.
- **FR-006**: `enqueue`'s existing calls MUST keep working; tasks of 0.7 in the queue MUST keep
  running.

### Key Entities

- **Task status**: state, task name, attempts, last error type, dead letter reason, enqueued and
  updated times, the unique key's digest.
- **Unique key**: a digest of the key, holding the id of the task that has it.

## Success Criteria *(mandatory)*

- **SC-001**: Unit tests for each requirement; live tests on Redis, Valkey, their clusters and a
  tiered backend for unique keys, status, cancel and timeouts.
- **SC-002**: The background guide has sections for each; the CHANGELOG lists them.

## Assumptions

- Operators run the command line where the app's settings (the backend's URL, the keys) are.

## Out of Scope

- Priorities, results and workflows ("Small on purpose" in the guide).
- Cancelling a running task.
