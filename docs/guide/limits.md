# Rate and concurrency limits

How many requests a client may make in a window, and how many may run at the same time, across every
process.

In an app: [the example](../examples/limits.md). Every class and function: [the API reference](../reference/limits.md).

## Rate limiting

`FastAPIRateLimiter` is a FastAPI dependency that lets each client make `times` requests per
window to a route; the next ones get a `429 Too Many Requests` with `Retry-After`. Its counters
live in the cache's backend, so every process and container shares them:

```python
from functools import partial

from fastapi import APIRouter, Depends
from starlette.requests import HTTPConnection

from fastapi_alcazaba.rate_limit import FastAPIRateLimiter


@app.get("/search", dependencies=[Depends(FastAPIRateLimiter(times=10, minutes=1))])
async def search(q: str) -> list[Product]: ...


def user_id(connection: HTTPConnection) -> str:  # a Request, or a WebSocket
    return connection.state.user_id  # set by your authentication middleware


# 1000 requests per hour and user, for all the routes of the router together.
api = APIRouter(
    dependencies=[Depends(FastAPIRateLimiter(1000, hours=1, name="api", identifier=user_id))]
)

# One budget across routes, of which each export takes 50.
partner_budget = partial(FastAPIRateLimiter, 1000, hours=1, name="partners", identifier=user_id)
partners = APIRouter()
partners.get("/orders", dependencies=[Depends(partner_budget())])(list_orders)
partners.get("/export", dependencies=[Depends(partner_budget(cost=50))])(export_orders)
```

- **Who:** by default, the client's address as the ASGI server reports it (`client_ip`), IPv6
  addresses by their /64 network: each customer gets at least a whole /64, and one budget per
  address would give a client 2⁶⁴ of them. Many ISPs delegate a /56 or a /48, and tunnel brokers
  hand out /48s for free: `client_network(24, 48)` groups wider networks. Behind
  a proxy, let the server take the address from the proxy's headers: uvicorn reads them, but
  only from the addresses in `--forwarded-allow-ips` (127.0.0.1 by default), so give it your
  proxy's. The limiter never reads `X-Forwarded-For` itself, which any client can set.
  `identifier=` counts per user, API key… Identities are hashed before they reach the backend.
- **Where:** each route has its own budget, whatever its path parameters (those of a `Mount`
  too): `/items/1` and `/items/2` share one, and so does an endpoint mounted twice. Limiters with the same `name` and rate share one budget across routes,
  and `cost=` makes a route take more of it.
- **How:** a sliding window. The count of the previous window is added in proportion to how much
  of it is still less than a window ago, so nobody doubles the limit around the boundary between
  two windows. Requests over the limit don't count: checking and counting is one atomic
  operation (a Lua script on Redis, Valkey and GLIDE, a conditional write on DynamoDB). A client
  with nothing left is then refused by each process without asking the backend, until something
  fits again: a flood doesn't reach the backend. `limiter.forget()` drops that memory (between
  tests; a new cache drops it too). Windows follow the clocks of the app's servers: keep them in
  sync (NTP).
- **What clients see:** responses carry the `RateLimit` and `RateLimit-Policy` headers of
  the [IETF draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/):
  `RateLimit: "api";r=998;t=3540` means 998 requests left, at least for the next 3540 seconds.
  Clients can slow down before they get a 429. `headers=False` leaves them out. An endpoint that
  returns a `Response` object itself gets none: FastAPI only adds the headers that dependencies
  set to the responses it builds from a return value. Nor do the errors it builds from an
  exception (a 422, an `HTTPException` raised after the limiter), unless `FastAPIProblemDetails`
  is installed: it adds them back. An invalid request counts too: FastAPI runs the dependencies
  before it answers 422.
- **Which backend:** the `cache=` given, else the app's (`Cache.install`) or `FastAPICache`'s. Every
  backend can keep counters; `TieredBackend` keeps them only in its shared backend, and
  `FastAPICache.clear()` never resets them. They go through the cache's timeout and circuit
  breaker: if the backend fails, requests go through (`fail_open=False` answers 503 instead).
- **What happens over the limit:** `callback(request, seconds_to_wait)`, by default
  `too_many_requests` (the 429). If the callback returns, the request goes on.

On a WebSocket route, the dependency limits connections (closing with 1008). Call
`await limiter.hit(connection, key=..., cost=...)` yourself to limit anything else: it counts and
returns `None` within the limit, else the seconds to wait. For example, the messages of a WebSocket, or
the login attempts to an account:

```python
import unicodedata

from fastapi_alcazaba.rate_limit import FastAPIRateLimiter, too_many_requests

login_attempts = FastAPIRateLimiter(times=5, minutes=15, identifier=lambda _: "")
login_attempts_per_client = FastAPIRateLimiter(times=20, minutes=15)  # per IP


@app.post("/login", dependencies=[Depends(login_attempts_per_client)])
async def login(request: Request, form: Annotated[LoginForm, Form()]) -> Token:
    # Five attempts per account every 15 minutes, wherever they come from. The key is the
    # account as the app finds it, or "Alice@x.com", "alice@x.com " and the like would each get
    # five more.
    account = unicodedata.normalize("NFKC", form.username).strip().casefold()
    if (wait := await login_attempts.hit(request, key=account)) is not None:
        too_many_requests(request, wait)
    ...
```

Normalise the key exactly as the login looks the account up. A limit per account also lets
anyone lock that account out for the window with five wrong passwords: pair it with the limit
per IP, and keep the window short.

## Concurrency limits

`FastAPIConcurrencyLimiter` limits how many requests run *at the same time*, across every process
and container, for endpoints whose cost is how long they run (AI, OCR, PDF rendering). The rate
limiter counts requests per window; this counts the ones in flight:

```python
from fastapi_alcazaba.rate_limit import FastAPIConcurrencyLimiter
from fastapi_alcazaba.rate_limit import client_ip

ocr = FastAPIConcurrencyLimiter(4, name="ocr", wait=2)  # FastAPICache's backend, or cache=...


@app.post("/invoices/scan", dependencies=[Depends(ocr)])
async def scan(file: UploadFile) -> Invoice: ...


@app.post("/summaries", dependencies=[Depends(FastAPIConcurrencyLimiter(1, identifier=client_ip))])
async def summarise(text: str) -> Summary: ...  # one at a time per client


async def scan_pending() -> None:  # outside requests: a job, a queue consumer
    async with ocr.slot():
        ...
```

- **Slots in the backend:** `limit` locks (up to 100), tried in random order so that the
  processes don't all queue on the first; a request holds one while it runs, renewed every
  `ttl / 3` seconds, and the slots of a process that dies expire after `ttl` (60). Keys are
  SHA-256 digests: client identities never reach the backend in clear.
- **When it's full:** a request waits up to `wait` seconds (0: tries once), polling with
  jittered backoff, then gets a 503 with `Retry-After` (`retry_after`, 1 s by default); a 429 when
  the limit is per client (`identifier`). There's no queue and no order among the waiters.
  WebSockets are closed with 1013 (1008 per client) and hold their slot while connected.
- **Sharing:** by default each route (methods and path template) has its own slots; limiters with
  the same `name` and `limit` share them. `slot(key=...)` gives each key its own slots outside
  requests (it needs a `name`), and `slot(connection=request)` takes one around just the
  expensive part of an endpoint.
- **Streaming:** the slot is released when FastAPI runs the dependency's exit code. Since FastAPI
  0.118 that's after the response is sent, so a `StreamingResponse` keeps its slot until its last
  byte; `Depends(limiter, scope="function")` (FastAPI 0.121) releases it when the endpoint
  returns, before the body. FastAPI 0.115 to 0.117 release it before the response is sent: take
  a `slot()` inside the stream there.
- **Failures:** if the backend fails, each process counts the slots on its own (`fail_open=True`,
  the default: at most `limit` per process run); `fail_open=False` refuses with a 503 instead
  (`slot()` raises `BackendUnavailable`). memcached can't hold locks.
- **Limits:** like any lock on a cache-like store, a holder that stalls for longer than `ttl`
  loses its slot while it still runs, and someone else may take it. Taking a slot when most are
  busy costs a round trip per busy slot tried.
