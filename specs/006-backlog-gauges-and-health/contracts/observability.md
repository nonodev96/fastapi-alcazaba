# Contract

```python
from fastapi_alcazaba.observability import BacklogMonitor
from fastapi_alcazaba.observability.health import (
    broadcast_check,
    health_router,
    leader_check,
    object_storage_check,
    smtp_check,
    task_queue_check,
)

monitor = BacklogMonitor(queue, outbox, interval=30, timeout=5)
app = FastAPI(lifespan=monitor.lifespan)

app.include_router(
    health_router(
        {
            "tasks": task_queue_check(queue, max_backlog=10_000, max_dead_letters=0),
            "broadcast": broadcast_check(broadcast),
            "leader": leader_check(leader),
            "smtp": smtp_check(sender),
            "storage": object_storage_check(presigner, "health/probe"),
        }
    )
)
```

New members: `FastAPITaskQueue.measure()`, `.counts()`; `SQLAlchemyOutbox.measure()`,
`.backlog()`; `FastAPIBroadcast.connected`; `FastAPILeader.campaigning`; `SMTPSender.check()`.
Each factory takes `critical=False` and `timeout=None`.
