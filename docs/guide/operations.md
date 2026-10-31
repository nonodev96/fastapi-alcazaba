# Operations

Switches every process sees at once: circuit breakers, maintenance mode and feature flags.

In an app: [the example](../examples/operations.md). Every class and function: [the API reference](../reference/operations.md).

## Circuit breakers

`FastAPIBreaker` stops calling a dependency that keeps failing (a database, an SDK, a service
reached without httpx), so that requests fail at once instead of waiting for it, and it gets room
to recover:

```python
from fastapi_alcazaba import FastAPIBreaker

payments = FastAPIBreaker("payments", failure_threshold=5, reset_timeout=30, timeout=10)


@payments
async def charge(order: Order) -> Receipt:
    return await payments_sdk.charge(order.id, order.total)


async def refund(order: Order) -> None:
    async with payments.attempt():  # or around a block
        await payments_sdk.refund(order.id)
```

- **States:** after `failure_threshold` consecutive failures it opens, and for `reset_timeout`
  seconds calls fail with `BreakerOpen` without reaching the dependency. Then a single trial call
  goes through: its success closes the breaker, its failure opens it again.
- **In FastAPI:** `BreakerOpen` is an `HTTPException`: an endpoint that hits an open breaker
  answers 503 with `Retry-After` (the seconds until the trial), without naming the dependency.
- **What counts:** exceptions in `failures` (any `Exception` by default) are failures; those in
  `exclude` mean the dependency answered (a "not found" from its SDK) and are successes; others,
  and a cancelled call, give no verdict. `timeout` bounds async calls: a slower call is
  cancelled, raises `TimeoutError` and counts as a failure. Sync code can't be interrupted, so a
  breaker with a timeout refuses to guard it.
- **Per process:** each process keeps its own state, with no round trip on every call. Sync
  functions work too, also from the threads of FastAPI's pool.
- **Closed by hand:** `payments.reset()` closes it at once and forgets the failures, when you know
  the dependency is back (from an admin endpoint) or between tests; `failure_threshold` and
  `reset_timeout` can be read back.
- **Observability:** opening is logged as a warning and closing again as info, and both are
  counted with the calls (`fastapi_alcazaba.breaker.*` metrics). `breaker.state` says
  `"closed"`, `"open"` or `"half-open"`, handy in a health check:
  `HealthCheck(lambda: payments.state != "open", critical=False)`.

## Maintenance mode

`FastAPIMaintenance` is a switch shared by every process; while it's on, `MaintenanceMiddleware`
answers 503 with `Retry-After` to every request but the allowed ones:

```python
from fastapi_alcazaba.rate_limit import client_ip
from fastapi_alcazaba.operations import FastAPIMaintenance, MaintenanceMiddleware

maintenance = FastAPIMaintenance()  # FastAPICache's backend; or FastAPIMaintenance(cache)
app.add_middleware(
    MaintenanceMiddleware,
    maintenance=maintenance,
    allow_paths=("/health", "/admin"),  # always served
    allow=lambda request: client_ip(request) in OFFICE,  # the team keeps working
)

await maintenance.enable("Back at 10:30", retry_after=900)  # from an admin route, a script...
await (
    maintenance.status()
)  # MaintenanceStatus(enabled=True, message=..., retry_after=900, since=...)
await maintenance.disable()
```

- **Shared, but not asked on every request:** the state lives in the backend, and each process
  keeps a copy refreshed at most every `refresh` seconds (1 by default), so a change reaches every
  process within about that. `status()` reads the backend now; `forget()` drops the copy, and a
  new cache drops it by itself (each test's lifespan sets one up).
- **Responses:** a 503 whose `detail` is the message ("Service Unavailable" without one), with
  `Retry-After` when given and `Cache-Control: no-store`. WebSockets are closed with 1013 (try
  again later). Lifespan events go through. A pure ASGI middleware: streaming isn't affected.
- **Exceptions:** `allow_paths` are prefixes (`/health` covers `/health/live`, not `/healthz`),
  matched without the `root_path` of a proxy or a mount, and are served without reading the state.
  `allow` (sync or async) receives the request: use the address the server reports
  (`client_ip`), never `X-Forwarded-For`.
- **Failures:** when the backend fails, each process keeps the last state it read (maintenance
  often means work on the backend itself: its failure mustn't reopen an app closed on purpose)
  and serves normally if it never read one. `enable()` and `disable()` raise
  `BackendUnavailable`: nothing changes then.
- **Integrity:** with a `signing_key` or `encryption_key` on the cache the state is sealed, and a
  state planted in the backend by someone else is ignored; the message is capped at 1000
  characters.

## Feature flags

`FastAPIFeatureFlags` keeps flags in the backend: turn features on and off for everybody, for some
users or tenants, or for a percentage of them, from any process and without a deploy:

```python
from fastapi_alcazaba.operations import FastAPIFeatureFlags

flags = FastAPIFeatureFlags()  # FastAPICache's backend; or FastAPIFeatureFlags(cache, refresh=5)

await flags.set("new-checkout", enabled=True, percentage=10, allow=["user-42"], deny=["user-7"])

if await flags.enabled("new-checkout", subject=str(user.id)):
    ...


async def user_id(user: Annotated[User | None, Depends(optional_user)]) -> str | None:
    return str(user.id) if user is not None else None  # who the request proved to be


@app.get("/beta/report", dependencies=[Depends(flags.require("beta-report", subject=user_id))])
async def beta_report() -> Report: ...  # 404 while the flag is off for the user
```

- **Rules:** a flag is on for a subject when it's enabled, the subject isn't in `deny`, and it's
  in `allow` or falls within `percentage`. No percentage means everybody, but only `allow` when
  there's an allow list: a flag for a few never goes to everyone by mistake. `enabled=False` turns it off for
  everyone, `allow` included. Without a subject, a partial rollout counts as off, and so does a
  flag with a deny list. An undefined flag gives `default` (False).
- **The subject is who the request proved to be** (the user of your authentication), never a
  header or a parameter the client writes: whoever sends `x-user-id: user-42` would get the
  allow list's features, and could pick a value inside any rollout.
- **Sticky rollouts:** a subject's place comes from a hash of the flag's name and the subject, so
  it keeps its answer while the percentage stays, raising the percentage only adds subjects, and
  each flag picks different subjects.
- **Hidden when off:** `require()` answers FastAPI's own `404 Not Found`, so the flag isn't
  revealed, and closes WebSockets with 1008. For subjects that need other dependencies (the
  current user), call `enabled()` from your own dependency.
- **Local copies:** each process keeps the definitions it reads for `refresh` seconds (5), so a
  change reaches every process within that, and a flag costs at most one round trip per
  `refresh`. When the backend fails, the last definition read is used, or the default.
  `get(name)` reads the backend now; `delete(name)` removes a flag; `forget()` drops the copies,
  and a new cache drops them by itself.
- **Privacy and integrity:** subjects are stored as SHA-256 digests salted with the flag's name,
  never in clear (at most 10,000 per list). Whoever reads the backend can still confirm a guess
  of a guessable subject (an e-mail address): prefer opaque ids, or give the cache an
  `encryption_key`, which encrypts the definitions. With a `signing_key` or `encryption_key`,
  definitions planted by someone else are ignored. Names are up to 100 letters, digits, `.`, `_`
  and `-`.
