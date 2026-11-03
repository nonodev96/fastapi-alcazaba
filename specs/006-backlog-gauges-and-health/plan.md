# Implementation Plan: Backlog gauges and ready-made health checks

**Branch**: `feature/backlog-gauges-and-health` | **Date**: 2026-11-03 | **Spec**: [spec.md](spec.md)

## Summary

Gauges in `Metrics` (R1), set by breakers and leaders; a `BacklogMonitor` that measures queues
and outboxes (R2); a scheduler duration histogram (R3); five health check factories (R4–R6).

## Technical Context

**Language/Version**: Python 3.11–3.14 · **Dependencies**: none new · **Testing**: pytest,
OpenTelemetry SDK's in-memory reader, fakeredis, SQLite, a local SMTP stub, a local HTTP server

**Scale/Scope**: `telemetry/metrics.py`, `observability/backlog.py` (new), `observability/health.py`,
`background/{tasks,outbox,leader,broadcast,scheduler}.py`, `operations/breaker.py`,
`mail/sending.py`; docs

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Secure by default | Checks answer ok/fail only; attributes are the app's names | Pass |
| II. Tests | Every gauge, the monitor, every check | Pass (tasks) |
| III. Quality | ruff, mypy --strict, pylint | Pass (tasks) |
| IV. Shared foundations | `Metrics`, `HealthCheck`, the router's timeouts | Pass |
| V. Docs and changelog | observability.md, production.md, CHANGELOG | Pass (tasks) |
| VI. Simplicity | Sync gauges; one monitor; no new dependency | Pass |

## Changes to existing behaviour

None for apps that don't start a monitor nor use the checks; breakers and leaders report two
more instruments.
