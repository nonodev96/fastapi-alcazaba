# Background work

A sign-up service whose work goes on after the response. `POST /users` saves the user and hands
the welcome e-mail to [`FastAPITaskQueue`](../guide/background.md#task-queue), which runs it in the background,
in whichever process takes it, and retries it while the e-mail provider is down. Out of retries, it
becomes a dead letter, which an admin endpoint lists and puts back in the queue once the provider
is back. [`FastAPIScheduler`](../guide/background.md#scheduled-jobs) ends the trials on the hour and sends the
team a sign-up report on weekdays at 8:00 in Madrid, each run in one process only. And
[`FastAPILeader`](../guide/background.md#leader-election) has one process relay the outbox, the events saved
with the users, to the message broker; if that process stops, another one takes over.

Unlike the other examples, it needs Redis or Valkey: the tasks live in Redis streams that every
process shares. They're signed with the cache's `signing_key`, so a task planted or altered in
Redis is refused and never runs.

## Run it

From a clone of the repository, with Redis or Valkey running (a throwaway one:
`docker run --rm -p 127.0.0.1:6379:6379 valkey/valkey:8`):

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
export REDIS_URL=redis://127.0.0.1:6379/0
export CACHE_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ADMIN_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
uvicorn examples.background.main:app
```

Every process needs the same `CACHE_SIGNING_KEY`, or the tasks that one seals are refused by the
others. The admin routes take `Authorization: Bearer $ADMIN_TOKEN`; without `ADMIN_TOKEN` the token
is random, and they stay closed. The database, the e-mail provider and the broker are stand-ins in
the process's memory, so try it with one process. With a real database, run as many as you need.

## Try it

A sign-up answers at once, before the welcome e-mail goes out. A moment later the queue has sent
it:

```console
$ curl -i -X POST http://127.0.0.1:8000/users -H "Content-Type: application/json" \
    -d '{"email": "ana@example.com", "name": "Ana"}'
HTTP/1.1 201 Created
content-type: application/json

{"id":1,"email":"ana@example.com","name":"Ana","created_at":"2026-10-26T11:36:58.884780Z","plan":"trial","welcomed":false}

$ curl http://127.0.0.1:8000/users/1
{"id":1,"email":"ana@example.com","name":"Ana","created_at":"2026-10-26T11:36:58.884780Z","plan":"trial","welcomed":true}
```

What the process is doing: it's the leader, no task is pending, and the jobs run next (in UTC) on
the hour and at 8:00 in Madrid, which is 7:00 UTC in winter time. Without the token, a 401:

```console
$ curl http://127.0.0.1:8000/admin/background -H "Authorization: Bearer $ADMIN_TOKEN"
{"leader":true,"pending_tasks":0,"next_runs":{"end-trials":"2026-10-26T12:00:00Z","signup-report":"2026-10-27T07:00:00Z"}}

$ curl -i http://127.0.0.1:8000/admin/background
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer
content-type: application/json

{"detail":"Not authenticated"}
```

Now the e-mail provider goes down. A sign-up still answers at once, and its task waits for a
retry:

```console
$ curl -X PUT "http://127.0.0.1:8000/admin/simulate/mailer?down=true" \
    -H "Authorization: Bearer $ADMIN_TOKEN"

$ curl -X POST http://127.0.0.1:8000/users -H "Content-Type: application/json" \
    -d '{"email": "luis@example.com", "name": "Luis"}'
{"id":2,"email":"luis@example.com","name":"Luis","created_at":"2026-10-26T11:36:59.914617Z","plan":"trial","welcomed":false}

$ curl http://127.0.0.1:8000/admin/background -H "Authorization: Bearer $ADMIN_TOKEN"
{"leader":true,"pending_tasks":1,"next_runs":{"end-trials":"2026-10-26T12:00:00Z","signup-report":"2026-10-27T07:00:00Z"}}
```

The retries come 10 s, 20 s, 40 s… apart, with jitter. After the sixth attempt, here about four
minutes later, the task is a dead letter. It says which task failed and the type of its error,
never its arguments nor the error's message, which may hold personal data:

```console
$ curl http://127.0.0.1:8000/admin/dead-letters -H "Authorization: Bearer $ADMIN_TOKEN"
[{"id":"1793014866323-0","reason":"failed","task":"send-welcome-email","task_id":"dd4b818df71444c0af26dcc01e59ce10","error":"examples.background.main.MailerUnavailable","attempts":6,"failed_at":"2026-10-26T11:41:06.323669Z"}]
```

Once the provider is back, the dead letter goes back in the queue, and the e-mail goes out:

```console
$ curl -X PUT "http://127.0.0.1:8000/admin/simulate/mailer?down=false" \
    -H "Authorization: Bearer $ADMIN_TOKEN"

$ curl -X POST http://127.0.0.1:8000/admin/dead-letters/requeue \
    -H "Authorization: Bearer $ADMIN_TOKEN"
{"requeued":1}

$ curl http://127.0.0.1:8000/users/2
{"id":2,"email":"luis@example.com","name":"Luis","created_at":"2026-10-26T11:36:59.914617Z","plan":"trial","welcomed":true}

$ curl http://127.0.0.1:8000/admin/dead-letters -H "Authorization: Bearer $ADMIN_TOKEN"
[]
```

## What to look at

- **At least once, so safe to run twice:** a worker that dies after sending the e-mail, before it
  can say so, has its task run again elsewhere. `send_welcome_email` checks first whether the
  e-mail already went out.
- **Only an ID goes through Redis:** the task takes the user's ID and reads the address when it
  runs, so no personal data waits in Redis, and the log line names the user by ID too.
- **Sealed tasks:** the cache's `signing_key` signs every task and binds it to its queue. Without
  it, whoever can write to Redis could run the tasks with arguments of their choosing.
- **Each run in one process:** the scheduler's ticks are the same in every process
  (`every(hours=1)` is on the hour, UTC), and only one of them runs each tick. The cron job has a
  time zone, so the report is at 8:00 in Madrid in summer time and in winter time.
- **One relay, never two:** two processes relaying the outbox would publish every event twice.
  The leader's loop is cancelled if its process stops leading, and starts in the next leader. An
  event leaves the outbox only once it's published: a crash in between publishes it again, and
  never loses it.
- **Started and stopped with the app:** `async with queue, leader, scheduler` in the lifespan.
  They stop in the reverse order: the jobs first, the queue last, giving its running tasks 10 s.

## The code

<!-- fmt: off -->
```python title="examples/background/main.py"
--8<-- "examples/background/main.py"
```
<!-- fmt: on -->
