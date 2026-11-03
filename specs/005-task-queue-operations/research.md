# Research: A task queue operators can keep healthy

Findings from reading `fastapi_alcazaba/background/tasks.py`, its tests, `outbox.py` and
`docs/guide/background.md`, on 2026-11-03.

## R1. Timeouts

- **Decision**: `task(timeout=None)`: each run under `asyncio.timeout`. Past it, `TimeoutError`
  goes through the failure path (retry or dead letters, `error` = `TimeoutError`), logged as a
  timeout. A sync task's `to_thread` is abandoned: its slot is freed, the thread can't be stopped.
- **Rationale**: heartbeats keep a running task from being taken over, so a hung one held its slot
  forever. The timeout is per task because tasks differ by orders of magnitude.

## R2. Unique keys

- **Decision**: `enqueue(..., unique_key=...)` takes a key `{base}:unique:<sha256 of the queue
  and the key>` holding the task id, in the enqueue script: if it's held, the script returns its
  id and adds nothing. It expires at the task's deadline (`until`), refreshed by each retry. The
  ack, bury and cancel scripts delete it only if it still holds that task's id. The task carries
  the digest (`unique`), so that whoever settles it knows the key.
- **Rationale**: atomic in one script; a digest (SHA-256, not keyed: the cache's protector only
  seals and opens) because keys may be personal data (an e-mail); the
  conditional delete keeps a cancelled task's late settlement from releasing a newer task's key.
- **Compatibility**: a 0.7 worker runs such a task and leaves the key; it expires at the deadline.

## R3. Which errors retry

- **Decision**: `retry_on` (None: all) and `give_up_on` (none), tuples of exception types. An
  exception in `give_up_on`, or not in `retry_on` when it's given, sends the task to the dead
  letters at once (reason `failed`).

## R4. Status records

- **Decision**: a hash `{base}:status:<task id>`: `state`, `task`, `attempts`, `error`, `reason`,
  `enqueued`, `updated`, `unique`. Written inside the scripts that move the task (enqueue, claim,
  ack, retry, bury, requeue, cancel), so it never disagrees with the queue. It expires `status_ttl`
  (1 day) after the task ends, and that long past its deadline while it hasn't.
- **States**: `pending` (waiting, delayed, requeued, back after a shutdown), `running`, `retrying`
  (waiting for its next attempt), `succeeded`, `dead` (with `reason`), `cancelled`.
- **What it leaves out**: the arguments. A replayed copy (refused) doesn't write the real task's
  record; malformed or tampered tasks have no trusted id.
- **Rationale**: tasks are stream entries found by entry id; nothing found them by task id.

## R5. Cancelling

- **Decision**: `cancel(id)` marks a `pending` or `retrying` record `cancelled` (and releases its
  unique key) in one script. The claim script, which every run goes through, refuses a cancelled
  task: the worker drops it (acks it) without running it. A `running` task can't be cancelled.
- **Rationale**: removing the entry would need its stream id or its payload (the delayed set's
  member); a mark checked atomically at claim time is enough, and the race with a worker starting
  it has one winner. The cancelled entry counts in `pending()` until a worker drops it.

## R6. The command line

- **Decision**: `[project.scripts] fastapi-alcazaba = "fastapi_alcazaba.cli:main"`, argparse, one
  group `tasks`: `fastapi-alcazaba tasks TARGET COMMAND`. `TARGET` is `module:attribute` (a queue,
  or a function, sync or async, that returns one after setting up the cache) or `module` (its
  queues). Commands: `queues`, `pending`, `dead-letters [-n]`, `requeue (--all | ID...)`,
  `discard ID...`, `status ID`, `cancel ID`; `--json`. The current directory is importable, as with
  uvicorn.
- **Rationale**: a queue that uses FastAPICache has no backend until the app's lifespan sets it
  up; a factory lets the app say how, without the CLI running the lifespan (which would start
  workers). `requeue` wants `--all` so that no one requeues everything by forgetting the ids.
- New queue method `dead_letter_count()`, for `queues`.

## R7. Metrics

- **Decision**: `tasks` gets the outcome `cancelled` (dropped by a worker without running). A
  timeout keeps the outcome of the failure it is (`retried` or `failed`); the log says it ran past
  its timeout, and its error type is `TimeoutError`.

## R8. Format

- **Decision**: still format 2; `unique` is an optional string. 0.7 workers ignore it.
