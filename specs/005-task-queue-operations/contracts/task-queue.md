# Contract: task queue operations

```python
@queue.task(retries=5, timeout=30, retry_on=(httpx.TransportError,), give_up_on=(ValueError,))
async def send_invoice(invoice_id: int) -> None: ...


task_id = await queue.enqueue(send_invoice, 42, unique_key="invoice-42")
same = await queue.enqueue(send_invoice, 42, unique_key="invoice-42")  # == task_id while it lives

status: TaskStatus | None = await queue.status(task_id)
status.state  # "pending" | "running" | "retrying" | "succeeded" | "dead" | "cancelled"
await queue.cancel(task_id)  # True if it hadn't started
await queue.dead_letter_count()
```

`TaskStatus`: `id`, `task`, `state`, `attempts`, `error`, `reason`, `enqueued_at`, `updated_at`.

`FastAPITaskQueue(status_ttl=86400)`.

```text
fastapi-alcazaba tasks myapp.worker queues
fastapi-alcazaba tasks myapp.worker:queue pending
fastapi-alcazaba tasks myapp.worker:queue dead-letters [-n 20]
fastapi-alcazaba tasks myapp.worker:queue requeue --all | ID [ID ...]
fastapi-alcazaba tasks myapp.worker:queue discard ID [ID ...]
fastapi-alcazaba tasks myapp.worker:queue status TASK_ID
fastapi-alcazaba tasks myapp.worker:make_queue cancel TASK_ID
  --json   machine-readable output
```

Exit status: 0; 1 when a task isn't found or can't be cancelled; 2 for usage errors.
