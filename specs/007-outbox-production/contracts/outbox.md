# Contract

```python
outbox = SQLAlchemyOutbox(outbox_table(metadata), sessions=async_session)
relay = OutboxRelay(outbox, deliver, retention=7 * 86400, purge_interval=3600, purge_batch=1000)

await outbox.purge(older_than=7 * 86400)  # rows deleted
await outbox.given_up(limit=100)  # list[GivenUpMessage]
await outbox.requeue(["<id>"])
await outbox.requeue()  # count
await outbox.discard(["<id>"])  # count
```

`GivenUpMessage`: `id`, `topic`, `attempts`, `created`, `failed`, `error`.
`OutboxMessage.traceparent`: the W3C context of where it was written, or None.
