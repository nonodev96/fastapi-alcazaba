# Backends

Where the cache and every other tool keep their entries and their state.

Every class and function: [the API reference](../reference/backends.md).

- **InMemoryBackend(max_size=10_000):** per process; for development, tests and single-process
  apps. Up to `max_size` cached responses, and as many keys of state (counters, revoked tokens…)
  in a store of their own.
- **RedisBackend(redis):** redis-py client, standalone or cluster, created without
  `decode_responses=True`. Works with any server that speaks the Redis protocol (Valkey, KeyDB,
  Dragonfly).
- **ValkeyBackend(valkey):** the same with valkey-py (`valkey.asyncio`, not the sync client).
- **GlideBackend(client):** Valkey GLIDE, the official Valkey client with a Rust core
  (`await GlideClient.create(...)` or `GlideClusterClient`).
- **TieredBackend(remote, local_ttl=5):** a per-process memory level in front of any of these
  (see "Two-level cache").
- **MemcachedBackend(client):** aiomcache. Can't tell the remaining time to live.
- **DynamoBackend(table_name, region, endpoint_url=None):** aiobotocore; `await backend.init()`
  first. Enable TTL on the `ttl` attribute of the table. `endpoint_url` for DynamoDB Local or
  LocalStack.

| Backend | Clear a namespace | Locks | Tags | Rate limiter |
| --- | --- | --- | --- | --- |
| InMemory | yes | (one process) | yes | (one process) |
| Redis, Valkey, GLIDE | yes (`SCAN` + `UNLINK`) | yes | yes | yes, one Lua script |
| Memcached | no | no | no | yes, adding and taking back what doesn't fit |
| DynamoDB | no | yes (conditional writes) | no | yes, a conditional write |
| Tiered | like its shared backend | like its shared backend | like its shared backend | in its shared backend |

Valkey example with valkey-py:

```python
import valkey.asyncio

from fastapi_alcazaba.backends.valkey import ValkeyBackend

client = valkey.asyncio.from_url("valkey://localhost:6379/0")
FastAPICache.init(ValkeyBackend(client), prefix="myapp", signing_key=settings.cache_signing_key)
```

## Two-level cache

```python
from fastapi_alcazaba.backends.tiered import TieredBackend

FastAPICache.init(TieredBackend(ValkeyBackend(client), local_ttl=5), prefix="myapp")
```

`TieredBackend` keeps a small copy of each entry in the memory of every process, in front of the
shared backend: hits served from there skip the network (131 µs → 56 µs per hit against a Valkey
on the same machine; more across a real network). A local copy lives at most `local_ttl` seconds
and never longer than the shared entry.

With Redis or Valkey through redis-py/valkey-py, writes, clears and tag invalidations are also
announced on a pub/sub channel (`invalidation_channel`), so every process drops its copy at once:
a tag invalidation names the keys it deleted, also the copies of processes that only read those
entries (beyond 1000 keys, every process drops all its copies). A value read while an
invalidation arrives isn't kept, since it may be the old one. Copies are only kept while the
process is subscribed: if the subscription drops, or the client reconnects by itself, the local
level is emptied, and bypassed until it's back. The channel is read with its own timeout, so a
client `socket_timeout` doesn't end the subscription, and pinged after 5 quiet seconds: a
connection that died silently is given up within 10. With the other backends (memcached,
DynamoDB, GLIDE, a valkey-py cluster) staleness is bounded by `local_ttl`. If the shared backend
goes down, the local copies keep being served until they expire.
