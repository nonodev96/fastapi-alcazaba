# Background work

Work outside requests, shared by every process: jobs at given times, a task queue, a leader, and
locks.

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
  keeps a run that outlasts its interval from overlapping the next tick in another process
  (memcached can't hold locks: there, each tick still runs once, but long runs may overlap).
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
  down when its lock expires: no leader is better than two. memcached can't hold locks.
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
  script on Redis, Valkey and GLIDE and by conditional writes on DynamoDB (in memory, within one
  process). memcached can't hold locks. On DynamoDB, expiry compares the clocks of the app's
  servers: keep them in sync (NTP). A custom backend written for 0.1 has no `extend_lock`: use
  `renew=False` there, with a `ttl` longer than the work.
- **Waiting:** `wait=0` (the default) tries once, `wait=5` keeps trying for 5 seconds,
  `wait=None` as long as it takes. `acquire()` and `release()` work without `async with`.
- **Failures:** if the backend fails, `acquire()` raises `BackendUnavailable`: without it nobody can
  tell who holds the lock.
- **Limits:** a holder that stalls for longer than `ttl` (a long pause, a network partition) loses
  the lock while it still runs. `lock.held` turns False when the renewals couldn't keep up: check it
  between the steps of long jobs, and make work that must never overlap idempotent too.
