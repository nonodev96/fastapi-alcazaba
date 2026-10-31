# Errors and observability

Errors that say what went wrong without leaking internals, and what tells you how the app is doing:
request IDs, deadlines, health checks, metrics and traces.

In an app: [the example](../examples/observability.md). Every class and function: [the API reference](../reference/observability.md).

## Problem details

`FastAPIProblemDetails` answers errors with RFC 9457 problem details
(`application/problem+json`) instead of FastAPI's `{"detail": ...}`, and without telling clients
what they shouldn't know:

```python
from fastapi_alcazaba.observability import FastAPIProblemDetails, Problem

problems = FastAPIProblemDetails()
problems.install(app)


@app.post("/orders")
async def order(item: Item) -> Order:
    if stock[item.id] < item.quantity:
        raise Problem(
            409,
            f"Only {stock[item.id]} left",
            type="https://example.com/problems/out-of-stock",
            title="Out of stock",
            extensions={"available": stock[item.id]},
        )
    ...
```

```json
{"type": "https://example.com/problems/out-of-stock", "title": "Out of stock", "status": 409,
 "detail": "Only 3 left", "instance": "/orders", "available": 3, "request_id": "4bf92f35…"}
```

- **Members:** `type` (`about:blank` unless a `Problem` says otherwise), `title` (the RFC 9110
  status phrase), `status`, `detail` when there's something to tell, `instance` (the request's
  path, percent-encoded) and `request_id` when `RequestIDMiddleware` gave the request one.
- **HTTPExceptions:** FastAPI's, Starlette's and their subclasses (`BreakerOpen`, the rate
  limiter's 429…) keep their headers: `Retry-After`, `WWW-Authenticate`, `Allow`. A 4xx's detail
  is sent, since it's written for clients (`expose_detail=False` keeps it); a 5xx's never is: an
  `HTTPException(500, str(error))` becomes a bare 500, and the hidden detail is logged as a
  warning. A detail that isn't a string goes in an `errors` member. Codes under 400 (a redirect
  raised as an exception) keep FastAPI's answer.
- **Validation errors:** a 422 whose `errors` list each error's `loc`, `msg` and `type`, never
  its `input` (FastAPI echoes what was sent, passwords included) nor its `ctx`. At most
  `max_errors=100`, so that a huge invalid body doesn't get a huge answer.
- **Unhandled exceptions:** a 500 that says nothing about them. Starlette raises the exception
  again after the response, so the server logs it with its traceback (uvicorn's "Exception in
  ASGI application", with the request ID when you use `RequestIDLogFilter`) and `TestClient`
  raises it in tests; it isn't logged a second time. With `debug=True` Starlette shows its
  traceback page instead.
- **Problem:** an `HTTPException` with a `type` URI, a `title` and extension members, whose names
  are checked (three or more letters, digits or `_`, starting with a letter; never a standard
  member) and whose values are encoded when it's raised. Its detail is always sent, also with a
  5xx: you wrote it for the client. `problems.response(request, Problem(...))` answers one from
  your own exception handlers.
- **Alcazaba's middlewares too:** what they answer themselves (the 504 of a timeout, the 413 of a
  body too large, the 503 of maintenance, the 403 of CSRF, the idempotency errors) is a problem
  as well, headers such as `Retry-After` kept, once it's installed on the app.
- Handlers you register for a status code or an exception subclass still win; install it before
  the app starts.

## Request IDs

`RequestIDMiddleware` gives every request an ID, to find all the log lines of one request and to
let a client quote it when it reports an error:

```python
import logging

from fastapi_alcazaba.observability import RequestIDLogFilter, RequestIDMiddleware, request_id

app.add_middleware(RequestIDMiddleware)  # last: the outermost

handler = logging.StreamHandler()
handler.addFilter(RequestIDLogFilter())
handler.setFormatter(logging.Formatter("%(asctime)s [%(request_id)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(handler)


@app.post("/jobs")
async def create_job() -> dict[str, str | None]:
    return {"ticket": request_id()}
```

With uvicorn, add the filter to its handlers too (`"filters": {"request_id": {"()":
"fastapi_alcazaba.observability.RequestIDLogFilter"}}` in its `log_config`), and its access and
error lines carry the ID.

- **Incoming IDs:** the request's `X-Request-ID` (`header=` for another) is reused when a proxy
  or the client set one that's safe to log: a single value matching `[A-Za-z0-9._:-]{1,128}`.
  Anything else (line breaks that would forge log lines, an endless value, several values) gets
  a new random one, 128 bits in hex. `trust_incoming=False` always makes a new one.
- **Everywhere in the request:** `request_id()` returns it in endpoints, dependencies, sync
  endpoints' threads and WebSockets; None outside a request. The response carries it in the
  same header (unless the app set that header itself), and so do WebSocket handshakes.
- **In errors:** after an unhandled exception the ID stays set for the rest of the request's
  task, so Starlette's 500 handler and the server's error log, which run outside every
  middleware, still know which request failed. `FastAPIProblemDetails` puts it in every problem,
  500s included, and in their header.
- **In logs:** `RequestIDLogFilter` sets `record.request_id` (`default="-"` outside a request; a
  `request_id` passed in `extra=` is kept). Add it to handlers: a logger's filters don't see the
  records of its child loggers.
- Add it last, the outermost, so that the answers of the other middlewares (a 413, a 504) carry
  the ID too.

## Request timeouts

`RequestTimeoutMiddleware` gives every request a deadline, so that a hung dependency or a client
that trickles its body doesn't hold a request (and its database connection, its memory…)
forever:

```python
from fastapi_alcazaba.observability import RequestTimeoutMiddleware

app.add_middleware(
    RequestTimeoutMiddleware,
    timeout=30,
    paths={"/reports": 120, "/events": None},  # Server-Sent Events: no deadline
)
```

- **Before the response starts:** at the deadline the app is cancelled, its `finally` blocks and
  the exit of its `yield` dependencies run, and the client gets a 504
  (`{"detail": "Request timed out"}`, or a problem with `FastAPIProblemDetails`). Reading the
  request body counts: a client that sends it a
  byte at a time runs out of time too.
- **While a response streams:** it's too late for a 504. The app is cancelled all the same and
  the TimeoutError raised again, so the server closes the connection: the client sees an
  incomplete body (an unfinished chunked read), never a short one that looks complete. Give
  streaming endpoints their own deadline in `paths`, or None.
- **After the response:** once its last byte is sent the deadline is off, so background tasks run
  as long as they need.
- **Sync endpoints** run in a thread that can't be interrupted: the client gets its 504 on time,
  but the thread runs to its end, after the exit of its `yield` dependencies has run. Endpoints
  that may hit the deadline are better async.
- **Paths:** `paths` sets other deadlines by path prefix (the longest wins, None for none),
  matched like the routes, without the `root_path`; `timeout=None` leaves the other paths
  without one. The app's own TimeoutErrors go through untouched, and WebSockets and lifespan
  events too.
- Both cases are logged as warnings with the method and the route template. A plain ASGI
  middleware: streaming responses still stream.

## Health checks

`health_router()` adds the two endpoints that load balancers and orchestrators probe:

```python
from fastapi_alcazaba.observability import HealthCheck, health_router


async def database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


app.include_router(
    health_router({"database": database, "search": HealthCheck(ping_search, critical=False)})
)
```

Point the liveness probe at `/health/live` and the readiness probe (or the load balancer's
health check, e.g. the target group of an ECS service) at `/health/ready`.

- **Live vs ready:** `/health/live` only says that the process answers. `/health/ready` runs the
  checks concurrently, each with a timeout (`timeout=2.0`, or the check's own), and answers 200
  (`ok`, or `degraded` when only non-critical checks fail) or 503 (`fail`) with each check's
  status and duration. Keep dependencies out of liveness: a probe that checks the database gets
  healthy containers restarted every time it blips. A check passes when it returns anything but
  `False` without raising. Sync checks run in a daemon thread of their own, off the event loop,
  so one that hangs holds up neither the shutdown nor the threads of the loop's default executor;
  it isn't started again, under the same name in the same router, until its thread finishes.
- **The cache is checked, but isn't critical:** the cache's backend (`cache=`, else the app's or
  `FastAPICache`'s) is asked for a key under its prefix, straight, not through the circuit
  breaker. The cache fails open, so its outage makes the app `degraded`, not unready: taking
  every instance out of the load balancer would turn a Redis outage into a full one.
  `include_cache=False` leaves it out.
- **No leaked errors:** failures are logged with their traceback and never returned: a public
  probe mustn't tell the world the database's hostname or connection string.
- **Probes don't pile up:** concurrent `/ready` requests share one run of the checks, and its
  result answers the next ones for `reuse_for=1.0` seconds, so a flood of probes doesn't flood
  the database. A probe that goes away doesn't cancel the run the others are waiting for.
- The endpoints are left out of the OpenAPI schema unless `include_in_schema=True`; responses
  carry `Cache-Control: no-store`.

## Metrics and traces

Alcazaba logs under `fastapi_alcazaba.*` with a `NullHandler`, as libraries do: its warnings (a
breaker that opens, maintenance turned on, a backend that fails) show once the app configures
logging, e.g. `logging.basicConfig(level=logging.INFO)`. uvicorn alone only configures its own
loggers.

Alcazaba only uses the OpenTelemetry API (the `otel` extra): it records nothing until the app
configures an SDK, and never configures one itself. The simplest way is OpenTelemetry's own
zero-code instrumentation, with the standard variables:

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install  # instrumentations for FastAPI, httpx2, redis, SQLAlchemy…

OTEL_SERVICE_NAME=shop-api \
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production \
OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.internal:4317 \
OTEL_PYTHON_LOG_CORRELATION=true \
OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="health/live,health/ready" \
opentelemetry-instrument uvicorn app:app
```

Give the collector's endpoint explicitly, over TLS: traces and logs carry what your app does.
Then Alcazaba adds to them:

- **Spans:** one per cached call (`fastapi_alcazaba <function>`, with `fastapi_alcazaba.result`:
  hit, miss, refresh or bypass) and, inside it, one per backend call (`fastapi_alcazaba.backend
  get`, `set`, `lock`…, with failures recorded); instrumented Redis/Valkey clients nest their own
  inside. Each run of a scheduled job is the root of a trace of its own
  (`fastapi_alcazaba.scheduler <job>`, failures recorded), so what the job does (queries, calls)
  is grouped under it instead of scattered. So is each run of a task (`fastapi_alcazaba.task
  <task>`, with its queue, ID and attempt), linked to the span that enqueued it.
- **On the request's span:** the events `fastapi_alcazaba.rate_limit` (a request limited or
  unchecked), `fastapi_alcazaba.breaker.refused`, `.opened` and `.closed`,
  `fastapi_alcazaba.outbound.refused` (an outbound call to an origin whose breaker is open) and
  `fastapi_alcazaba.csrf.refused`, and the attribute `fastapi_alcazaba.idempotency` with the
  outcome of an Idempotency-Key.
- **Never:** keys, tokens, identities, codes or request bodies. Attributes are names, routes and
  outcomes, few enough to keep the cardinality of the metrics low.

| Metric | Attributes |
| --- | --- |
| `fastapi_alcazaba.lookups` | `fastapi_alcazaba.result` (hit, miss, refresh, bypass), `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.stores` | `fastapi_alcazaba.outcome` (stored, uncacheable, too_large, unencodable, failed), `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.entry.size` | `fastapi_alcazaba.namespace` |
| `fastapi_alcazaba.backend.duration` | `fastapi_alcazaba.operation` (get, set, delete, incr, lock, unlock, extend_lock, tag, incr_window) |
| `fastapi_alcazaba.backend.errors` | `fastapi_alcazaba.operation`, `error.type` |
| `fastapi_alcazaba.rate_limit` | `fastapi_alcazaba.outcome` (allowed, limited, unchecked), `fastapi_alcazaba.limiter` (its name, or the route) |
| `fastapi_alcazaba.breaker.calls` | `fastapi_alcazaba.outcome` (success, failure, refused), `fastapi_alcazaba.breaker` |
| `fastapi_alcazaba.breaker.transitions` | `fastapi_alcazaba.state` (open, closed), `fastapi_alcazaba.breaker` |
| `fastapi_alcazaba.scheduler.runs` | `fastapi_alcazaba.outcome` (ran, failed, skipped, overlapping, unchecked), `fastapi_alcazaba.job` |
| `fastapi_alcazaba.idempotency` | `fastapi_alcazaba.outcome` (stored, replayed, conflict, mismatch, not_kept, invalid, too_large, unchecked) |
| `fastapi_alcazaba.tokens.checks` | `fastapi_alcazaba.outcome` (valid, revoked, unchecked) |
| `fastapi_alcazaba.otp.verifications` | `fastapi_alcazaba.outcome` (success, wrong, missing, exhausted, malformed, unchecked): a burst of wrong or exhausted is someone guessing |
| `fastapi_alcazaba.lock.wait` | `fastapi_alcazaba.acquired` (true, false): seconds spent acquiring locks |
| `fastapi_alcazaba.lock.lost` | locks whose renewals couldn't keep up |
| `fastapi_alcazaba.health.checks` | `fastapi_alcazaba.check`, `fastapi_alcazaba.status` (ok, fail) |
| `fastapi_alcazaba.health.check.duration` | `fastapi_alcazaba.check` |
| `fastapi_alcazaba.concurrency` | `fastapi_alcazaba.outcome` (acquired, local: counted in the process while the backend failed, refused, unavailable), `fastapi_alcazaba.limiter` |
| `fastapi_alcazaba.leader.transitions` | `fastapi_alcazaba.leader`, `fastapi_alcazaba.event` (elected, lost, resigned) |
| `fastapi_alcazaba.maintenance.refused` | requests refused during maintenance |
| `fastapi_alcazaba.flags.evaluations` | `fastapi_alcazaba.flag`, `fastapi_alcazaba.result` (on, off, default) |
| `fastapi_alcazaba.problems` | `fastapi_alcazaba.status`, `fastapi_alcazaba.kind` (http_exception, problem, validation, unhandled) |
| `fastapi_alcazaba.request_timeouts` | `fastapi_alcazaba.outcome` (504, cut: already streaming), `fastapi_alcazaba.route` |
| `fastapi_alcazaba.request_ids` | `fastapi_alcazaba.source` (incoming, generated, rejected): many rejected may be a misconfigured proxy, or someone forging log lines |
| `fastapi_alcazaba.outbound.breaker` | `fastapi_alcazaba.event` (opened, refused), `server.address` |
| `fastapi_alcazaba.outbound.ssrf.blocked` | `fastapi_alcazaba.reason` (scheme, host, port, address) |
| `fastapi_alcazaba.webhooks` | `fastapi_alcazaba.outcome` (accepted, replayed, rejected, malformed, too_large, unchecked, unavailable) |
| `fastapi_alcazaba.api_keys` | `fastapi_alcazaba.outcome` (accepted, malformed, unknown, wrong_secret, expired, insufficient_scope) |
| `fastapi_alcazaba.auth` | `fastapi_alcazaba.strategy`, `fastapi_alcazaba.outcome` (success, failure: credentials there but refused, unauthenticated: no strategy found a user) |
| `fastapi_alcazaba.authz` | `fastapi_alcazaba.outcome` (allowed, denied, unauthenticated): a burst of denied is someone probing |
| `fastapi_alcazaba.csrf.refused` | `fastapi_alcazaba.kind` (http, websocket), `fastapi_alcazaba.reason` (fetch_site, origin, no_source, token) |
| `fastapi_alcazaba.sessions` | `fastapi_alcazaba.event` (created, updated, rotated, deleted, touched, rejected: forged or altered, unreadable, unavailable) |
| `fastapi_alcazaba.uploads` | `fastapi_alcazaba.outcome` (accepted, too_large, too_many_pixels, unsupported, unreadable, empty) |
| `fastapi_alcazaba.uploads.size` | `fastapi_alcazaba.content_type` (the detected one): bytes of the uploads accepted |
| `fastapi_alcazaba.tasks.enqueued` | `fastapi_alcazaba.queue`, `fastapi_alcazaba.task` |
| `fastapi_alcazaba.tasks` | `fastapi_alcazaba.queue`, `fastapi_alcazaba.task`, `fastapi_alcazaba.outcome` (succeeded, retried, failed, interrupted; for refused tasks malformed, tampered, too_large, unknown_task, invalid_arguments, abandoned, with the task only when it's registered) |
| `fastapi_alcazaba.tasks.duration` | `fastapi_alcazaba.queue`, `fastapi_alcazaba.task`, `fastapi_alcazaba.outcome` |
| `fastapi_alcazaba.tasks.reclaimed` | `fastapi_alcazaba.queue`: tasks taken over from workers that stopped answering |
| `fastapi_alcazaba.audit` | `fastapi_alcazaba.action`, `fastapi_alcazaba.outcome` (success, failure, denied): a burst of denied is someone probing |
| `fastapi_alcazaba.audit.undelivered` | `fastapi_alcazaba.sink`, `fastapi_alcazaba.reason` (error, timeout): events a sink didn't write |
