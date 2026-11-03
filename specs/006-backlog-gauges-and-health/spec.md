# Feature Specification: Backlog gauges and ready-made health checks

**Feature Branch**: `feature/backlog-gauges-and-health`

**Created**: 2026-11-03

**Status**: Draft

**Input**: User description: "Operators should be able to alert on work piling up and see in
health checks whether each Alcazaba component is usable, without writing their own polling code."

## Clarifications

### Session 2026-11-03

No question was put to the maintainer, who asked for the feature to be built and merged. The
choices a clarification would have asked about are recorded, with their reasons, in research.md:

- Values the process knows (a breaker's state, whether it leads) are synchronous gauges set when
  they change; values that cost backend or database calls (queue and outbox backlogs) are measured
  by a `BacklogMonitor` the app starts, every `interval` (30 s), and by nothing otherwise (R1, R2).
- Scheduler job durations are a histogram, like task durations (R3).
- The health check factories return `HealthCheck`s that aren't critical by default: a backlog or
  an SMTP outage degrades the app, it doesn't take it out of the load balancer (R4).
- The leader check passes while this process campaigns: "nobody leads" is an alert on the sum of
  the `is_leader` gauge across processes, which no single process can see (R5).
- The object storage check reads one object the app names, with a URL it signs: the credentials
  the presigner has may only read and write objects (R6).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Alert on work piling up (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a `BacklogMonitor(queue, outbox)` started, **When** the metrics are read, **Then**
   there are the tasks queued and delayed, the dead letters, the outbox's unsent messages and the
   age of its oldest one, refreshed every `interval`.
2. **Given** no monitor, **Then** no backend call is made for them.
3. **Given** a source that fails, **Then** it's logged and the others are still measured.

---

### User Story 2 - See breakers, leadership and job durations (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a `FastAPIBreaker` that opens, **Then** `breaker.open` is 1 for it, 0 once closed.
2. **Given** a `FastAPILeader`, **Then** `leader.is_leader` is 1 in the process that leads and 0
   in the others; their sum across processes is 1 when someone leads.
3. **Given** a scheduled job, **Then** each run's duration is recorded, by job and outcome.

---

### User Story 3 - Ready-made health checks (Priority: P2)

**Acceptance Scenarios**:

1. **Given** `health_router({"tasks": task_queue_check(queue, max_dead_letters=0)})`, **When**
   the queue has a dead letter, or the backend doesn't answer, **Then** the check fails.
2. **Given** `broadcast_check(broadcast)`, **When** its subscription is reconnecting or Redis
   doesn't answer, **Then** it fails.
3. **Given** `leader_check(leader)`, **When** the leader isn't campaigning (not started, or its
   campaign died), **Then** it fails.
4. **Given** `smtp_check(sender)` or `object_storage_check(presigner, key)`, **When** the server
   doesn't answer, refuses the credentials, or the object can't be read, **Then** it fails.
5. **Given** any of them failing, **Then** the response says only "fail": no host, no error.

### Edge Cases

- A queue on a backend without streams in the monitor: logged once per run, not fatal.
- Metrics disabled (no OpenTelemetry): the gauges are no-ops; an app without metrics has no
  reason to start a monitor, and the guide says so.
- An outbox with no unsent message: backlog 0, oldest age 0.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Gauges `tasks.backlog` (by queue and state: queued, delayed), `tasks.dead_letters`,
  `outbox.backlog`, `outbox.oldest_age`, `breaker.open`, `leader.is_leader`; histogram
  `scheduler.duration`. Attributes are names the app gives, never values from requests.
- **FR-002**: Backlogs are measured only by a started `BacklogMonitor`, every `interval`, each
  source with a timeout.
- **FR-003**: Health check factories `task_queue_check`, `broadcast_check`, `leader_check`,
  `smtp_check`, `object_storage_check`, usable in `health_router`, with a timeout and no secret
  in their output.
- **FR-004**: Nothing changes for apps that use none of it.

## Success Criteria *(mandatory)*

- **SC-001**: Tests with the in-memory reader of OpenTelemetry for every gauge and the histogram;
  tests for every check passing and failing.
- **SC-002**: observability.md documents the gauges, the monitor and the checks; production.md
  has example alerts.

## Assumptions

- Metrics are exported by the app's OpenTelemetry SDK, as the counters already are.

## Out of Scope

- Dashboards and alert rules of a given product (Grafana, Datadog).
- Health checks for the database (the app's own).
