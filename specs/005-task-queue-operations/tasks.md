# Tasks: A task queue operators can keep healthy

**Input**: Design documents from `specs/005-task-queue-operations/`

**Tests**: required by the constitution (principle II).

## Phase 1: Foundational

- [X] T001 Status records written by the scripts (enqueue, claim, ack, retry, bury, requeue) in
  `fastapi_alcazaba/background/tasks.py`; `TaskStatus`, `status(id)`, `status_ttl`

## Phase 2: User Story 1 — Timeouts (P1)

- [X] T002 [US1] `task(timeout=...)`; tests: async and sync tasks past it, slot freed, retried
  then dead with `TimeoutError`

## Phase 3: User Story 2 — Unique keys (P1)

- [X] T003 [US2] `enqueue(..., unique_key=...)`; released on success, dead letters, cancel;
  refreshed by retries; tests incl. concurrent enqueues and delayed tasks

## Phase 4: User Story 3 — Which errors retry (P2)

- [X] T004 [US3] `retry_on`, `give_up_on`; tests

## Phase 5: User Story 4 — Status and cancel (P2)

- [X] T005 [US4] `cancel(id)` and the claim's check; `dead_letter_count()`; tests of every state
  change, the race with a worker, replayed copies, 0.7 tasks

## Phase 6: User Story 5 — Command line (P3)

- [X] T006 [US5] `fastapi_alcazaba/cli.py`, `[project.scripts]`; tests in `tests/unit/test_cli.py`

## Phase 7: Polish

- [X] T007 Live tests in `tests/integration/test_tasks_live.py`
- [X] T008 Typing test; metrics description; background guide; reference; README; CHANGELOG
- [X] T009 `make lint`, unit tests with coverage, integration, `mkdocs build --strict`
