# Contract: what each backend does after this feature

| Backend | Clear a namespace | Locks | Tags | Remaining lifetime | Rate limiter |
| --- | --- | --- | --- | --- | --- |
| InMemory | yes | (one process) | yes | yes | (one process) |
| Redis, Valkey, GLIDE | yes (`SCAN` + `UNLINK`), counted | yes | yes, counted | yes | yes |
| memcached | yes (generations), returns 0 | yes (`add`, `gets` + `cas`; whole seconds, never shorter) | yes (generations), returns 0 | yes | yes |
| DynamoDB | yes (generations), returns 0 | yes (conditional writes) | yes (generations), returns 0 | yes | yes |
| Tiered | like its shared backend | like its shared backend | like its shared backend | yes | in its shared backend |

## Guaranteed

1. A lock has one holder; only the holder releases or extends it; it expires on its own, never
   before the time asked.
2. After `clear(namespace=...)` or `invalidate_tags(...)` returns, no entry stored under it before
   is served (DynamoDB: once its eventually consistent reads see the clear).
3. A clear never hides or deletes state (`Cache.state_key`).
4. A value the backend can't read as an entry of this version is a miss, never an error.
5. `invalidate_tags_with_keys` on memcached and DynamoDB returns `(0, None)`.

## Unchanged

The `Backend` interface; InMemory, Redis, Valkey, GLIDE; `TieredBackend`; how state keys are stored
on memcached and DynamoDB; the DynamoDB table layout.
