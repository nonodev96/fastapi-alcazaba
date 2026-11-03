# Research: Backlog gauges and ready-made health checks

Findings from reading `telemetry/metrics.py`, `observability/health.py`, `background/` (tasks,
outbox, leader, broadcast, scheduler), `operations/breaker.py`, `mail/sending.py` and
`uploads/s3.py`, and the OpenTelemetry API 1.27+, on 2026-11-03.

## R1. Synchronous gauges, set when the value changes

- **Decision**: `GAUGES` (name -> unit, description) beside `COUNTERS`, and `Metrics.set(name,
  value, **attributes)` on `meter.create_gauge` (stable in the API since 1.27, the extra's floor).
  A breaker sets `breaker.open` at each transition; a leader sets `leader.is_leader` when it
  starts, is elected, loses or resigns.
- **Rationale**: observable gauges call back synchronously, so they can't make backend calls;
  values the process holds are cheapest set where they change.

## R2. The backlog monitor

- **Decision**: `BacklogMonitor(*sources, interval=30, timeout=5)` in
  `fastapi_alcazaba.observability`, with `start`/`stop`/`lifespan`. Each source has `async
  measure()` (`FastAPITaskQueue`, `SQLAlchemyOutbox`), run every `interval`, each within
  `timeout`; a failure is logged and the others go on. Without a monitor, nothing is measured:
  "off by default where measuring costs backend calls".
- **Sources**: the queue reads XLEN of its stream, ZCARD of its delayed set and XLEN of its dead
  letters in one script; the outbox runs one query (count and min(created) of the unsent).
- **Rationale**: one place, one interval, every process may run it (gauges are per process; the
  values are the same: alert on max).

## R3. Scheduler durations

- **Decision**: histogram `scheduler.duration` (s), by job and outcome (ran, failed), recorded
  for runs that started.

## R4. Health check factories

- **Decision**: in `observability/health.py`, each returns a `HealthCheck(critical=False)` (a
  `critical=` and `timeout=` override it):
  - `task_queue_check(queue, max_backlog=None, max_dead_letters=None)`: the counts (which also
    proves the backend answers), against the thresholds.
  - `broadcast_check(broadcast)`: PING through its client; fails while its subscription
    reconnects (`FastAPIBroadcast.connected` is False).
  - `leader_check(leader)`: `FastAPILeader.campaigning`.
  - `smtp_check(sender)`: `SMTPSender.check()`: connect, TLS, login, NOOP, QUIT.
  - `object_storage_check(presigner, key)`: a GET of the first byte of `key` with a URL it signs.
- **Rationale**: the router already runs checks with a timeout, in a thread for sync ones, and
  returns only ok/fail, logging the errors: no secret reaches the response.

## R5. Leadership health

- **Decision**: a process passes while it campaigns (its campaign task is alive). "Nobody leads"
  is `sum(fastapi_alcazaba.leader.is_leader) == 0`, an alert across processes.

## R6. Object storage

- **Decision**: GET with `Range: bytes=0-0` (unsigned header) of a presigned URL for `key`; 200 or
  206 pass, anything else (403 for bad credentials or a missing object without ListBucket, 5xx, no
  answer) fails. Stdlib `urllib` in the check's thread: no new dependency. The app uploads a small
  object for it (the guide says so).

## R7. Names

- **Decision**: `fastapi_alcazaba.tasks.backlog` (state: queued, delayed),
  `fastapi_alcazaba.tasks.dead_letters`, `fastapi_alcazaba.outbox.backlog`,
  `fastapi_alcazaba.outbox.oldest_age` (s), `fastapi_alcazaba.breaker.open`,
  `fastapi_alcazaba.leader.is_leader`, `fastapi_alcazaba.scheduler.duration` (s); attributes with
  the `fastapi_alcazaba.` prefix as the counters'. OpenTelemetry has no semantic convention for
  these (messaging's `messaging.*` are for clients of brokers, not a queue's depth).
