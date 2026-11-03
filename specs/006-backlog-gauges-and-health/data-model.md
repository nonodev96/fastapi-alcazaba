# Data Model: instruments

| Instrument | Kind | Unit | Attributes | Set by |
| --- | --- | --- | --- | --- |
| `tasks.backlog` | gauge | {task} | queue, state (queued, delayed) | BacklogMonitor |
| `tasks.dead_letters` | gauge | {task} | queue | BacklogMonitor |
| `outbox.backlog` | gauge | {message} | outbox (table name) | BacklogMonitor |
| `outbox.oldest_age` | gauge | s | outbox | BacklogMonitor |
| `breaker.open` | gauge | 1 | breaker | FastAPIBreaker, on transitions |
| `leader.is_leader` | gauge | 1 | leader | FastAPILeader |
| `scheduler.duration` | histogram | s | job, outcome | FastAPIScheduler |
