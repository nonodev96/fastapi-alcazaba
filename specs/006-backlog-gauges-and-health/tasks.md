# Tasks: Backlog gauges and ready-made health checks

- [X] T001 `GAUGES`, `Metrics.set`, histogram `scheduler.duration` in `telemetry/metrics.py`
- [X] T002 [US2] `breaker.open` on transitions; `leader.is_leader`; scheduler durations
- [X] T003 [US1] `FastAPITaskQueue.counts()`/`measure()`, `SQLAlchemyOutbox.backlog()`/`measure()`
- [X] T004 [US1] `observability/backlog.py`: `BacklogMonitor`
- [X] T005 [US3] `FastAPIBroadcast.connected`, `FastAPILeader.campaigning`, `SMTPSender.check()`
- [X] T006 [US3] Check factories in `observability/health.py`
- [X] T007 Tests: `tests/unit/test_gauges.py`, `tests/unit/test_health_checks.py`
- [X] T008 Docs: observability.md, production.md (alerts), reference; CHANGELOG; README
- [X] T009 `make lint`, unit tests with coverage, integration, `mkdocs build --strict`
