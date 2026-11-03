# Background work

Work outside requests, shared by every process: jobs at given times, a task queue, a leader,
locks, messages to every process, and an outbox for the messages a change announces.

In an app: [the example](../examples/background.md). Every class and function: [the API reference](../reference/background.md).

## Scheduled jobs

`FastAPIScheduler` runs jobs every N seconds or by a cron expression, in every process of the
app, and each tick once across them all:

```python
from fastapi_alcazaba import FastAPIScheduler

scheduler = FastAPIScheduler()  # FastAPICache's backend; or FastAPIScheduler(cache)


@scheduler.every(minutes=5)
async def refresh_catalog() -> None: ...


@scheduler.cron("0 3 * * *", tz="Europe/Madrid")
def nightly_report() -> None: ...  # sync jobs run in a thread


@scheduler.every(seconds=30, exclusive=False)
async def reload_settings() -> None: ...  # in every process: what each keeps in memory


app = FastAPI(lifespan=scheduler.lifespan)  # or `async with scheduler:` in your lifespan
```

- **Once per tick:** at each tick, the processes claim it in the backend, with an atomic counter
  named after the job and the tick, and only the first runs it. A lock held while the job runs
  keeps a run that outlasts its interval from overlapping the next tick in another process (on a
  backend of your own without locks, each tick still runs once, but long runs may overlap).
  The ticks are the same in every process: `every(minutes=5)` runs at :00, :05… (UTC), so keep
  the servers' clocks in sync (NTP). `exclusive=False` runs a job in every process.
- **Cron:** five fields (minute, hour, day of the month, month, day of the week, Sunday being 0 or
  7) with `*`, lists, ranges, steps and names (`*/15 9-17 * * mon-fri`), or `@hourly`, `@daily`,
  `@weekly`, `@monthly` and `@yearly`, in the time zone `tz` (UTC by default). When the clocks
  change it does what cronie does: a job at a fixed time runs an hour later on the day its hour
  is skipped and once on the day it repeats; a job with `*` in its minute or hour follows real
  time. No dependency: the parser is Alcazaba's.
- **No drift, no bursts:** the next run is the next tick after the clock, not a delay after the
  last run ended; ticks missed while a run took longer than its interval are skipped, not
  run in a row afterwards.
- **Failures:** an exception is logged with its traceback, counted, and the job keeps its
  schedule. If the backend fails, the tick is skipped (`fail_open=True` runs it anyway, maybe in
  every process).
- **Start and stop:** the jobs start with the app and never hold up its startup. At shutdown,
  jobs waiting for their next run stop at once and running ones get `grace` seconds (10) before
  they're cancelled; a sync job's thread runs to its end. `scheduler.next_run(name)` says when a
  job runs next. A job is named after its module and function (`name=` for another), the name
  that its ticks have in the backend.

## Task queue

`FastAPITaskQueue` hands work over from an endpoint to run in the background, in this process or
in any other that shares the cache's Redis or Valkey:

```python
from fastapi_alcazaba import FastAPITaskQueue

queue = FastAPITaskQueue()  # FastAPICache's backend; or FastAPITaskQueue(cache)


@queue.task(retries=5)
async def send_welcome_email(user_id: int) -> None: ...


app = FastAPI(lifespan=queue.lifespan)  # this process runs tasks too


@app.post("/users")
async def create_user(user: NewUser) -> None:
    ...
    await queue.enqueue(send_welcome_email, user.id)  # delay=60 or a timedelta to wait first
```

- **At least once:** tasks live in a Redis stream read by a consumer group, and leave it only once
  they've finished. A process that dies while running one, or can't reach the backend to say it
  finished, means that it runs again elsewhere: write tasks so that running one twice does no harm
  (check first whether the e-mail went out). A worker that stops answering has its tasks taken
  over after `visibility_timeout` (60 s); running tasks are kept from that by heartbeats, however
  long they run. A task handed out more than `max_deliveries` times (5) goes to the dead letters
  instead: it may be what kills the workers.
- **Only registered tasks run, with JSON arguments**, never pickle. `enqueue` checks that the
  arguments fit the function and serialise (TypeError otherwise), up to `max_payload_size` bytes
  (64 KiB); they arrive as JSON gives them back (tuples as lists).
- **Sealed:** with a `signing_key` or `encryption_key` on the cache, every task is signed or
  encrypted and bound to its queue. A task that someone planted in the backend or altered is
  refused, logged and sent to the dead letters, never run, and so is a copy of a sealed task
  written back to the queue: each delivery has a run id that only one entry may claim, until
  `replay_window` (7 days) after it was due, when it's refused as stale. Without a key a warning
  is logged at start: whoever can write to the backend can run your tasks with arguments of
  their choosing.
- **Retries:** a task that raises is tried again `retries` times (3), `backoff` seconds after the
  first failure (1), twice that after the second… up to `max_backoff` (600), each with jitter so
  that tasks that failed together (a service down) don't all retry at once. Then it goes to the
  dead letters with the type of its error, never the message, which may hold personal data.
  `dead_letters()` lists them, `requeue_dead_letters()` puts them back with their attempts reset,
  and `discard_dead_letters(ids)` deletes them.
- **Delayed tasks and retries** wait in a sorted set, and move to the stream when they're due in
  a Lua script: two processes never both move one.
- **Bounded:** the queue holds at most `max_length` tasks (100,000: waiting, running or delayed),
  and `enqueue` raises `QueueFull` beyond, instead of dropping any; about `max_dead_letters`
  (10,000) are kept. `pending()` counts the tasks that haven't finished.
- **Workers:** `start()` and `stop()`, `lifespan` or `async with`, running `concurrency` tasks at a
  time (10) in each process; sync functions run in a thread. `stop()` takes no more tasks and gives
  the running ones `grace` seconds (10); those still running then are cancelled and go back to the
  queue at once (a sync task's thread can't be stopped: it runs to its end, so that task may run
  twice). A process that only runs tasks: `async with queue: await asyncio.Event().wait()`.
- **Backends:** Redis 6.2+ or Valkey through `RedisBackend` or `ValkeyBackend` (or a
  `TieredBackend` in front of one), clusters included: a queue's keys share a hash tag. Other
  backends raise `NotImplementedError`. Its keys are state keys, which `clear()` never deletes.
- **Small on purpose:** no results, priorities or workflows. Each run is the root of a trace of its
  own, linked to the span of the request that enqueued it.

### Keeping it healthy

```python
@queue.task(
    retries=5,
    timeout=30,  # seconds a run may take
    retry_on=(httpx.TransportError,),  # only these are retried
    give_up_on=(ValueError,),  # these go to the dead letters at once
)
async def send_invoice(invoice_id: int) -> None: ...


task_id = await queue.enqueue(send_invoice, 42, unique_key="invoice-42")
await queue.status(task_id)  # TaskStatus(state="pending", attempts=0, ...)
await queue.cancel(task_id)  # True: it hadn't started
```

- **Timeouts:** a run that takes longer than `timeout` is cancelled and fails with `TimeoutError`:
  retried, or to the dead letters, and its worker takes other tasks. Without one, a task that hangs
  holds one of the `concurrency` slots for good: heartbeats keep it from being taken over. A sync
  task's thread can't be stopped: it's left to finish, outside the count.
- **Which errors retry:** `retry_on` lists those that are (all by default), `give_up_on` those that
  never are; either way the task goes to the dead letters at once, reason `failed`.
- **Unique tasks:** with `unique_key`, while a task enqueued with it waits, runs or waits for a
  retry, `enqueue` returns its ID and adds nothing: a message the outbox delivers twice, a request
  the client retries, run once. Once it succeeds, goes to the dead letters or is cancelled, the key
  is free (a requeued dead letter doesn't take it again). Many processes enqueuing with one key at
  once add one task. The backend keeps a digest of the key, not the key.
- **Status:** `status(task_id)` says where a task is: `pending` (waiting, delayed, requeued),
  `running`, `retrying` (waiting for its next attempt), `succeeded`, `dead` (with its `reason`)
  or `cancelled`, with its attempts, the type of its last error and when it was enqueued and last
  changed; never its arguments. Each task's record is written by the scripts that move the task,
  and kept `status_ttl` (a day) after it ends; then `status()` gives None.
- **Cancelling:** `cancel(task_id)` makes sure a task that hasn't started (pending, delayed or
  waiting for a retry) never runs, and frees its `unique_key`; a running or finished one isn't
  cancelled (False). A worker about to start it and `cancel` have one winner. The task counts in
  `pending()` until a worker drops it.
- **Inside a task:** `current_task()` gives its id, name, this run's attempt (1, 2…) and how
  many it may have, e.g. to record "the last attempt failed" without counting on your own.
- **Dead letters:** `dead_letter_count()` besides `dead_letters()`, `requeue_dead_letters()` and
  `discard_dead_letters()`.

### From a shell

`fastapi-alcazaba tasks` does the same from a shell, where the app's settings are. Point it at
the queue (`module:attribute`), or at a function that sets up what it needs and returns it: a queue
on FastAPICache has no backend until the app's lifespan runs, which the command doesn't (it would
start workers).

```python
# myapp/worker.py
async def queue_for_cli() -> FastAPITaskQueue:
    FastAPICache.init(
        RedisBackend(redis.from_url(settings.redis_url)),
        prefix="myapp",
        signing_key=settings.cache_signing_key,
    )
    return queue
```

```bash
fastapi-alcazaba tasks myapp.worker queues                     # every queue of the module
fastapi-alcazaba tasks myapp.worker:queue_for_cli pending
fastapi-alcazaba tasks myapp.worker:queue_for_cli dead-letters -n 20
fastapi-alcazaba tasks myapp.worker:queue_for_cli requeue --all  # or their IDs
fastapi-alcazaba tasks myapp.worker:queue_for_cli discard 1730000000000-0
fastapi-alcazaba tasks myapp.worker:queue_for_cli status 3f2a…
fastapi-alcazaba tasks myapp.worker:queue_for_cli cancel 3f2a…
```

`--json` (before the command) prints JSON. It exits with 1 when a task isn't found or wasn't
cancelled, and 2 for a wrong target or arguments. The current directory is importable, as with
uvicorn; `queues` needs the module's queues to have their cache (`FastAPITaskQueue(cache)`).

## Leader election

`FastAPILeader` elects one process among all those sharing the backend, for work that must run in
exactly one of them all the time (a queue consumer, an outbox relay, a watcher) rather than at
given times (that's the scheduler):

```python
from fastapi_alcazaba.background import FastAPILeader

leader = FastAPILeader("outbox")  # FastAPICache's backend; or FastAPILeader("outbox", cache=cache)


@leader.run
async def relay_outbox() -> None:
    while True:
        await relay_pending()
        await asyncio.sleep(1)


app = FastAPI(lifespan=leader.lifespan)  # or `async with leader:` in your lifespan

...
if leader.is_leader:  # anywhere, sync code included
    ...
```

- **Election:** every process campaigns for a lock named after the leader, renewed every
  `ttl / 3` seconds (`ttl` is 15 by default). The one that takes it leads and runs the functions
  given to `run`; the others try again every `ttl / 3` seconds or so, with jitter.
- **Hand-over:** a leader that stops releases the lock, and another process takes over within
  `ttl / 3` seconds; one that dies, within `ttl` and a third. A leader that loses the lock (its
  renewals couldn't keep up) cancels its functions within a second and campaigns again; at the
  next election they run again.
- **Functions:** async, without arguments, added before `start()`. They're cancelled when the
  leadership ends and must let the cancellation through. An exception is logged with its
  traceback; the process keeps leading.
- **Failures:** while the backend is down nobody is elected, and a leader that can't renew steps
  down when its lock expires: no leader is better than two.
- **Limits:** a leader that stalls for longer than `ttl` (a long pause, a network partition) can
  overlap the next one for a moment. Check `is_leader` between the steps of long work, and keep
  that work idempotent.

## Locks

A lock shared by every process and container, for work that only one of them must do at a time:

```python
from fastapi_alcazaba.background import LockNotAcquired


async def nightly_report() -> None:  # every instance runs it; one builds the report
    try:
        async with FastAPICache.lock("nightly-report", ttl=60):  # or cache.lock(...)
            await build_report()
    except LockNotAcquired:
        pass  # another instance is on it
```

- **Expiry and renewal:** a lock expires after `ttl` seconds, so a holder that dies doesn't keep it
  forever; while it's held, a background task renews it every `ttl / 3`.
- **Only the holder** releases or renews it: each acquisition has a random token, checked by a Lua
  script on Redis, Valkey and GLIDE, by conditional writes on DynamoDB and by `gets` + `cas` on
  memcached (in memory, within one process). memcached counts whole seconds: a lock there lasts
  up to two seconds more than its `ttl`, never less. On DynamoDB, expiry compares the clocks of
  the app's servers: keep them in sync (NTP). A custom backend written for 0.1 has no `extend_lock`: use
  `renew=False` there, with a `ttl` longer than the work.
- **Waiting:** `wait=0` (the default) tries once, `wait=5` keeps trying for 5 seconds,
  `wait=None` as long as it takes. `acquire()` and `release()` work without `async with`.
- **Failures:** if the backend fails, `acquire()` raises `BackendUnavailable`: without it nobody can
  tell who holds the lock.
- **Limits:** a holder that stalls for longer than `ttl` (a long pause, a network partition) loses
  the lock while it still runs. `lock.held` turns False when the renewals couldn't keep up: check it
  between the steps of long jobs, and make work that must never overlap idempotent too.

## Broadcast

!!! warning "Experimental"
    New in 0.7: its API may still change in any release.

What one process must tell the WebSockets of the others (a chat, a live board, notifications), or
the other processes themselves (a setting changed): `FastAPIBroadcast`, over Redis or Valkey's
pub/sub.

```python
from fastapi_alcazaba import FastAPIBroadcast

broadcast = FastAPIBroadcast(redis_client, signing_key=settings.broadcast_key)


@app.post("/rooms/{room}/messages")
async def say(room: int, form: Say, user: CurrentUser) -> None:
    await broadcast.publish(f"room:{room}", {"from": user.name, "text": form.text})


@app.websocket("/rooms/{room}")
async def listen(websocket: WebSocket, room: int) -> None:
    await websocket.accept()
    async with broadcast.subscribe(f"room:{room}") as messages:
        async for message in messages:
            if message.gap:
                await websocket.send_json(await room_state(room))  # some may have been missed
            else:
                await websocket.send_json(message.data)
```

- **At most once:** a message reaches those subscribed when it's published. One that a
  subscriber missed (its connection to Redis dropped, or it fell `queue_size` messages behind)
  doesn't come back: the subscriber gets a `gap` instead, and sends the state again. Work that
  must happen goes to the [task queue](#task-queue) or the [outbox](#outbox).
- **One subscription per process,** whatever its number of subscribers, which reconnects by
  itself, with a backoff.
- **Signed:** with `signing_key` (32 bytes or more, the same in every process; a list rotates
  it), each message carries an HMAC of its channel and data, and those without it, planted by
  whoever can write to Redis, are dropped.
- **Bounded:** a message is JSON, 64 KiB at most; channels are letters, digits and `_.:-`.
- **Without a client,** messages stay in the process: for tests, and apps of one process.
- **At shutdown,** `await broadcast.close()` in the lifespan.

## Outbox

A change saved and the message that announces it (to the task queue, a webhook, another service)
go together or not at all: the message is written in the same database transaction as the change,
and a relay delivers it afterwards, at least once. With the `sqlalchemy` extra.

```python
from fastapi_alcazaba.background import FastAPILeader
from fastapi_alcazaba.background.outbox import (
    OutboxMessage,
    OutboxRelay,
    SQLAlchemyOutbox,
    outbox_table,
)

outbox = SQLAlchemyOutbox(outbox_table(Base.metadata), sessions=async_session)


@app.post("/orders")
async def place(form: Order) -> dict[str, int]:
    async with async_session() as session, session.begin():
        order = Order(**form.model_dump())
        session.add(order)
        await session.flush()
        await outbox.add(session, "order.placed", {"order_id": order.id})  # same transaction
    return {"id": order.id}


async def deliver(message: OutboxMessage) -> None:
    # Delivered twice (the relay stopped before marking it sent): enqueued once.
    await queue.enqueue(order_placed, message.payload, unique_key=message.id)


relay = OutboxRelay(outbox, deliver)
leader = FastAPILeader("outbox")  # one process relays


@leader.run
async def relay_outbox() -> None:
    await relay.run()
```

- **The table** is yours, in your migrations: `outbox_table(metadata)` defines it (`outbox` by
  default). `sessions` makes the relay's sessions, a `sessionmaker` or an `async_sessionmaker`.
- **At least once:** `deliver(message)` is awaited, and the message marked as sent when it
  returns. A process that stops in between delivers it again: each message has an `id`, and
  consumers keep the ones they've seen.
- **Retries:** when `deliver` raises, the message is tried again 1, 2, 4… seconds later, up to
  `max_delay`, and given up on after `max_attempts`: kept in the table with its last error, never
  sent, for someone to look at.
- **In order, by one relay:** the oldest due messages first, `batch` at a time. Run the relay
  in one process ([leader election](#leader-election)).
- **Payloads** are JSON, 256 KiB at most: ids and what changed, not files.
- **The table doesn't grow without bound:** `run()` deletes the messages sent more than `retention`
  seconds ago (7 days), every `purge_interval` (an hour), `purge_batch` (1000) at a time, each
  batch in a short transaction of its own. Those given up on stay. `retention=None` keeps
  everything; `outbox.purge(older_than)` purges on demand.
- **Messages given up on** are listed by `outbox.given_up()` (id, topic, attempts, when written and
  given up, last error; not the payload), and `outbox.requeue(ids)` (all of them without ids) sends
  them again with their attempts reset, or `outbox.discard(ids)` deletes them.
- **Traced end to end:** a message keeps the trace context of where it was written (its
  `traceparent` column), and each delivery is a span of its own (`fastapi_alcazaba.outbox
  <topic>`) linked to it: from the request to the delivery, and on to the task the delivery
  enqueues, which links to the delivery. Failures are recorded on the span.
- **Counted:** `fastapi_alcazaba.outbox` (delivered, retried, given_up; by outbox and topic) and
  `fastapi_alcazaba.outbox.purged`; a [`BacklogMonitor`](observability.md#backlog-gauges)
  measures how many wait and since when. Metrics and traces go to FastAPICache's providers when
  it's set up, to the global ones otherwise.
- **Indexes:** `ix_<table>_due` on (`sent`, `next_try`), which serves the relay, the backlog and the
  purge, and `ix_<table>_failed`. Async sessions (`async_sessionmaker`, asyncpg) and plain ones
  (in a thread) work alike; the tests run on SQLite and PostgreSQL.
