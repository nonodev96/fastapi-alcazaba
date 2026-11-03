# Implementation Plan: A task queue operators can keep healthy

**Branch**: `feature/task-queue-operations` | **Date**: 2026-11-03 | **Spec**: [spec.md](spec.md)

## Summary

Per-task `timeout`, `retry_on` and `give_up_on` (R1, R3); `unique_key` and a status record per
task written by the queue's Lua scripts (R2, R4); `status`, `cancel` and `dead_letter_count`; a
`fastapi-alcazaba` command with a `tasks` group (R6).

## Technical Context

**Language/Version**: Python 3.11–3.14

**Primary Dependencies**: none new (argparse)

**Storage**: Redis/Valkey streams, sorted sets, strings and hashes of one hash tag

**Testing**: pytest with fakeredis (Lua); live on Redis, Valkey, clusters, tiered

**Project Type**: library + console script

**Constraints**: `enqueue`'s calls and 0.7's tasks keep working; every key of a queue in one slot

**Scale/Scope**: `fastapi_alcazaba/background/tasks.py` (kept in one module: the scripts and the
code that calls them belong together), `fastapi_alcazaba/cli.py` (new), `pyproject.toml`, docs

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Secure by default | No arguments in status; unique keys hashed; replayed copies don't write status; ids validated | Pass |
| II. Tests | Unit tests per requirement, live tests on every streams backend, CLI tests | Pass (tasks) |
| III. Quality | ruff, mypy --strict, pylint, signatures; typing test | Pass (tasks) |
| IV. Shared foundations | The queue's scripts, guard and metrics | Pass |
| V. Docs and changelog | Background guide, reference, CHANGELOG, README | Pass (tasks) |
| VI. Simplicity | No priorities, results or workflows; argparse | Pass |

## Changes to existing behaviour

- Each task writes a status record (a few hundred bytes, one day after it ends).
- A task can't take keyword arguments named `delay` or `unique_key`.

## Complexity Tracking

No constitution violations.
