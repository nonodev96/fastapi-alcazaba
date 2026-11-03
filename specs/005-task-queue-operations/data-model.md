# Data Model: task status, unique keys

Keys share the queue's base `{prefix}!tasks:{<digest of the name>}` (one hash slot).

## Status — `{base}:status:<task id>` (hash)

| Field | Meaning |
| --- | --- |
| `state` | pending, running, retrying, succeeded, dead, cancelled |
| `task` | the task's name |
| `attempts` | runs started |
| `error` | type of the last exception |
| `reason` | why it's dead (failed, abandoned, stale, unknown_task, invalid_arguments) |
| `enqueued` / `updated` | Unix times |
| `unique` | digest of its unique key, or "" |

Expires `status_ttl` after the task ends; while it hasn't, `status_ttl` past its deadline.

## Unique key — `{base}:unique:<sha256(queue name, key)>` (string)

The id of the task holding it; expires at the task's deadline, refreshed by retries; deleted when
it succeeds, goes to the dead letters or is cancelled, only if it still holds that id.

## Task body (format 2)

Adds `unique` (optional string): the digest above.

## State changes

| From | Event | To |
| --- | --- | --- |
| — | enqueue | pending |
| pending, retrying | claim | running (attempts + 1) |
| pending, retrying | cancel | cancelled (dropped at claim) |
| running | success | succeeded |
| running | failure with retries left | retrying |
| running | failure, last or given up, refused at claim | dead |
| running | stop() out of grace | pending |
| dead | requeue | pending (attempts 0) |
