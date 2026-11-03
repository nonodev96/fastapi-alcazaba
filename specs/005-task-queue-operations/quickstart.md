# Quickstart: checking it end to end

```bash
.venv/bin/pytest tests/unit/test_tasks.py tests/unit/test_tasks_operations.py tests/unit/test_cli.py
REDIS_URL=redis://127.0.0.1:16379/15 VALKEY_URL=valkey://127.0.0.1:16380/15 \
  CLUSTER_URL=redis://127.0.0.1:16382 .venv/bin/pytest -m integration tests/integration/test_tasks_live.py
```

By hand, with a Redis and a module `worker.py` holding a queue:

1. Enqueue a task twice with one `unique_key`: one id.
2. `fastapi-alcazaba tasks worker:queue status <id>`: pending; `cancel <id>`: cancelled.
3. A task with `timeout=1` that sleeps: dead after its retries, error `TimeoutError`;
   `fastapi-alcazaba tasks worker:queue dead-letters`, then `requeue --all`.
