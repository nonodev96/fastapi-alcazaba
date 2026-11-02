# Data Model: generations and the stamp

## Generation

| Field | Value |
| --- | --- |
| key | `fastapi_alcazaba!generation:namespace:<namespace>` or `fastapi_alcazaba!generation:tag:<tag key>` (a state key: no namespace of its own) |
| token | 16 random hex characters; replaced by every clear of that namespace or invalidation of that tag |
| lifetime | no expiry (memcached may evict it: R4) |

Stored as the raw token on memcached, as `{"key": ..., "generation": {"S": token}}` on DynamoDB.

## Namespaces of a key

Everything before each of the key's first 8 `:`; none for a state key (`is_state_key`).

```text
app:ns:abc        → app, app:ns
::abc             → "", ":"          (no prefix, no namespace)
app!lock:{x}      → (state)
```

## Stamp

What an entry records when it's stored (not for state keys):

| Field | memcached | DynamoDB | Meaning |
| --- | --- | --- | --- |
| expiry | `e`, Unix seconds, 0 for none | the existing `ttl` attribute | remaining lifetime on reads |
| generations | `g`: `{generation key: token}` for each namespace and tag | `generations` (namespaces) and `tag_generations` (tags) maps; "" for none | the entry is alive while each still matches |

memcached value: `\x00alcazaba-stamp:1\n` + `{"e":…,"g":{…}}` + `\n` + value.

### Alive

An entry is alive when, for every generation key in its stamp and every namespace of its key, the
token it recorded equals the current one ("" or absent on both sides counts as equal).

### Read rules

| Read | memcached | DynamoDB |
| --- | --- | --- |
| state key | raw, as today | as today |
| value with a valid stamp | alive → value and remaining lifetime; else a miss | same |
| value without a stamp (0.7, another app) | a miss | alive if no namespace was cleared, as today otherwise |
| malformed stamp | logged, a miss | — |
