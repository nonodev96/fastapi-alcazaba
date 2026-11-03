# Tasks: An outbox that runs for months

- [X] T001 dev group: aiosqlite, asyncpg, greenlet; postgres in `make services-up` and CI
- [X] T002 [US1] `purge()`; relay `retention`, `purge_interval`, `purge_batch` in `run()`
- [X] T003 [US2] `GivenUpMessage`, `given_up()`, `requeue()`, `discard()`
- [X] T004 [US3] `traceparent` column and `OutboxMessage.traceparent`; `Tracer.outbox`; counters
- [X] T005 Indexes `due` and `failed`
- [X] T006 [US4] Unit tests: sync and aiosqlite, `run()`; `tests/integration/test_outbox_live.py`
- [X] T007 Docs: background.md#outbox, observability.md, CHANGELOG; drop "Experimental" if all
  holds
- [X] T008 `make lint`, unit tests with coverage, integration, `mkdocs build --strict`
