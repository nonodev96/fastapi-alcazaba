# Accounting

!!! warning "Experimental"
    New in 0.6: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

In an app: [the example](../examples/accounting.md). Every class and function: [the API reference](../reference/accounting.md).

`FastAPIAccounting` meters what each subject uses: a user, an API key, a tenant. It counts each
meter (requests, tokens, rows, bytes…) per period, holds it to a quota, and sends a usage
record for each charge, which is what billing adds up. Authentication tells who makes the
request, authorization whether they may; accounting, how much they have used.

```python
from typing import Annotated

from fastapi import Depends

from fastapi_alcazaba import FastAPIAccounting
from fastapi_alcazaba.accounting import Metering, Quota


async def tokens_of(tenant: str) -> int:
    return (await plans.of(tenant)).tokens_a_month  # from your database


accounting = FastAPIAccounting(
    subject=current_tenant,  # your dependency: the tenant's ID, None without one
    quotas=[
        Quota("searches", limit=1_000, period="day"),
        Quota("tokens", limit=tokens_of, status=402),  # a month by default
    ],
)


@app.get("/search", dependencies=[Depends(accounting.meter("searches"))])
async def search(q: str) -> list[Result]: ...


@app.post("/summaries")
async def summarize(
    text: Text,
    usage: Annotated[Metering, Depends(accounting.meter("tokens", amount=None))],
) -> Summary:
    summary = await model.summarize(text)
    usage.add(summary.tokens)  # charged when the endpoint ends
    return summary
```

- **Two ways to charge:** `meter("searches")` charges 1 unit before the endpoint runs
  (`amount=5` for more), checked against the quota and counted in one atomic operation, so
  concurrent requests never go past it. `meter("tokens", amount=None)` gives the endpoint a
  `Metering` for what is only known as it goes (tokens, bytes, seconds), charged when it ends.
  Its quota is checked when the request starts: the requests running then may together go past
  it, by what each one uses.
- **Over the quota:** a 429 with `Retry-After`, the seconds until the period ends, or a 402
  (`status=402`: a plan to upgrade); a WebSocket is closed with 1008. The request isn't counted.
  Every response carries the IETF draft's `RateLimit` and `RateLimit-Policy` headers, named after
  the meter (`"searches";r=997;t=48600`), and problem details carry them too.
- **Plans:** `limit` is a number, or a function (plain or async) of the subject's ID that gives
  its plan's; None from it is no limit. Meters without a quota are counted, not bounded, per
  `period=` (a month by default).
- **Periods:** calendar hours, days or months, in UTC: a monthly quota starts again on the 1st
  at 00:00 UTC. `period_of("month", at)` gives a period's label (`2026-11`) and its bounds.
- **What's charged:** a client error (a 4xx, a validation error) was served, and stays charged:
  otherwise invalid requests would be free. A server error (an exception, a 5xx) or a client
  that went away is refunded, without a record. What an endpoint added to its `Metering` is
  charged whatever happens next: it was used.
- **Who:** `subject` is a dependency that returns an ID (a str, an int or a UUID), from your
  authentication: a user's, an API key's, a tenant's to share one quota among its users. Without
  one, a 401 (`WWW-Authenticate: <challenge>`). The counters' keys are SHA-256 digests: the IDs
  never reach the backend in clear.

## Usage and records

```python
@app.get("/usage")
async def read_usage(
    request: Request,
    tenant: Annotated[str, Depends(signed_in_tenant)],
) -> Usage:
    report = await accounting.usage(tenant, "tokens", request=request)
    return Usage(used=report.used, remaining=report.remaining, resets=report.resets)
```

- **Counters:** in the cache's backend (the app's, `get_cache`, or `cache=`), shared by every
  process, per subject, meter and period. `usage(subject, meter)` reads one: `used`, `limit`,
  `remaining`, `period` and when it `resets`; `at=` reads the period before, which they keep
  through the next one. `request=` takes the cache installed on the app.
- **Records:** each charge is a `UsageRecord` (a random `id`, `time`, `subject`, `meter`,
  `amount`, `period`, and the request's ID, trace, method and route template), sent to the
  `fastapi_alcazaba.accounting.usage` logger at INFO, one line of JSON, by default. That is what
  billing adds up, deduplicated by `id`: give the logger a handler and ship its lines, or pass
  `sinks=` of your own (any object with `async def write(record)`, such as one inserting into a
  table), `MemoryUsageSink` for tests, `()` for none. A sink that fails or takes longer than
  `timeout` is logged with the record, so that it isn't lost, and the others still get it.
- **Outside requests:** `await accounting.record(tenant, "tokens", 1200)` charges what a job or
  a webhook used, without checking the quota (it's been used), and sends its record.

## When the backend fails

The counters go through the cache's timeout and circuit breaker. When the backend fails, the
requests go through uncounted (`fail_open=True`, the default) and the quotas aren't checked, but
the records still have every charge: billing doesn't lose them. `fail_open=False` refuses them
with a 503 (a WebSocket, 1013) instead, for quotas that cost money to go past. `usage()` raises
`BackendUnavailable`.

- **Counted:** the requests in `fastapi_alcazaba.accounting`, by meter and outcome (allowed,
  exceeded, unauthenticated, unchecked, refunded), the units in
  `fastapi_alcazaba.accounting.usage` by meter, and the records not written in
  `fastapi_alcazaba.accounting.undelivered`.
- **And the rate limits:** a quota bounds what a subject uses in a calendar period, to bill or
  to share a resource; a [rate limit](limits.md) bounds how fast, in a sliding window, to protect
  the service. An API usually has both: `Depends(FastAPIRateLimiter(10, seconds=1))` before
  `Depends(accounting.meter("requests"))`, so that a burst is refused before it's charged.
