# Quickstart: checking it end to end

With the throwaway services up (`make services-up`):

```bash
MEMCACHED_URL=127.0.0.1:21211 DYNAMODB_URL=http://127.0.0.1:18000 \
  .venv/bin/pytest -m integration tests/integration/test_backends_live.py \
  tests/integration/test_coordination_live.py -k "memcached or dynamodb"
```

None of the lock, clear, tag or lifetime tests is skipped for memcached or DynamoDB.

By hand, on memcached:

```python
import aiomcache
from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends.memcached import MemcachedBackend

backend = MemcachedBackend(aiomcache.Client("127.0.0.1", 21211))
cache = Cache(backend, prefix="demo")

await backend.set("demo:products:1", b"v", expire=60)
await backend.get_with_ttl("demo:products:1")  # (60, b"v")
await cache.clear(namespace="products")  # 0: can't count
await backend.get("demo:products:1")  # None

async with cache.lock("nightly"):  # held once across processes
    ...
```
