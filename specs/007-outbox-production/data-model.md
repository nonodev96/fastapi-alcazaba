# Data Model: the outbox table

| Column | Type | Change |
| --- | --- | --- |
| id | String(32) PK | — |
| topic | String(200) | — |
| payload | Text | — |
| created | Float | — |
| attempts | Integer | — |
| next_try | Float | index dropped (in `due`) |
| sent | Float, null | index dropped (in `due`) |
| failed | Float, null | indexed (`ix_<table>_failed`) |
| error | String(500), null | — |
| traceparent | String(55), null | NEW (unless `traceparent=False`) |

Indexes: `ix_<table>_due (sent, next_try)`, `ix_<table>_failed (failed)`.

States: due (`sent` null), sent (`sent` set, `failed` null; purged after `retention`), given up
(`sent` and `failed` set; requeue → due; discard → deleted).
