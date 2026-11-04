# Provider budgets

!!! warning "Experimental"
    New in 0.9: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

Every class and function: [the API reference](../reference/accounting.md#provider-budgets).

What each user may cost the app at a provider it pays for: an LLM's tokens, SMS messages, OCR
pages, map requests. `FastAPIProviderBudget` holds each user, and the whole app, to a quota per
period, a rate, calls at the same time and a cap per call, with limits read from your database,
so that an admin panel changes them without a deploy. [Accounting](accounting.md) meters what
users use of your API; this, what they make you spend at someone else's.

```python
from typing import Annotated

from fastapi import Depends

from fastapi_alcazaba import FastAPIProviderBudget
from fastapi_alcazaba.accounting import BudgetLimits, ProviderCall

openai = FastAPIProviderBudget(
    "openai",
    subject=current_user_id,  # your dependency: the user's ID, None without one
    limits=limits_of,  # (user_id) -> BudgetLimits | None, from your database
    total=BudgetLimits(quota=50_000_000),  # the whole app's, a month
)


@app.post("/summaries")
async def summarize(
    text: Text,
    call: Annotated[ProviderCall, Depends(openai.reserve(2_000, at_least=100))],
) -> Summary:
    result = await llm.complete(text, max_tokens=call.reserved)
    call.use(result.usage.total_tokens)
    return result.summary
```

## Reserved before each call

- **Reserved, then settled:** `reserve(2_000)` takes 2000 units from the user's quota and from
  the app's before the endpoint runs, each in one atomic operation, and gives the endpoint a
  `ProviderCall`. Tell the provider not to use more (`max_tokens=call.reserved`), and say what it
  used (`call.use(...)`): when the endpoint ends, what it didn't use goes back to both quotas.
  Calls made at the same time, from any process, never go past either: twenty calls of 100 on a
  quota of 1000 make ten.
- **What's left, down to `at_least`:** with `at_least=100`, a user with 300 units left gets a
  call of 300 instead of a refusal; under 100, the refusal. For calls that can be made smaller.
- **Failures:** a call that raises before it `use`s anything gives everything back; what it
  used stays spent whatever happens next (the provider billed it). More than reserved is charged
  too, and logged: cap the call with `reserved`.
- **Refused:** a `BudgetRefused` (an HTTPException) with its `reason`:

| Reason | Status | When |
| --- | --- | --- |
| `disabled` | 403 | the user's limits (or the app's) say `enabled=False` |
| `quota`, `total` | 402 (`exceeded_status=429` for a 429) | the user's quota, or the app's, is spent; `Retry-After` when the period ends |
| `rate` | 429 | more than `rate` calls in `rate_window` seconds (a sliding window) |
| `concurrency` | 429 | `concurrency` calls running (`wait=` seconds to wait for one) |
| `unavailable` | 503 | the backend failed (with `fail_open=False`, the default), or the limits were never read |

- **Outside requests:** `async with openai.call(user_id, 2_000, at_least=100) as call:` in a task
  or a WebSocket, the same way; it raises `BudgetRefused`.
- **Headers:** the responses carry `RateLimit: "openai";r=<left>;t=<seconds>` and
  `RateLimit-Policy: "openai";q=<quota>`.

## The limits

```python
BudgetLimits(
    quota=1_000_000,  # units per period; None: no limit
    period="month",  # "hour", "day" or "month", in UTC
    rate=30,  # calls per rate_window seconds; None: no limit
    rate_window=60,
    concurrency=2,  # calls at the same time; None: no limit
    max_per_call=4_000,  # the most units one call reserves
    enabled=True,  # False: refused (a 403)
)
```

- **Per user:** `limits` is a `BudgetLimits` for everybody, or a function (plain or async) of the
  user's ID that reads them; None from it gives `default` (no limit, unless you give one).
- **The whole app:** `total` counts every user's calls together, with the same fields: its quota
  is what you're willing to spend this month, its rate and concurrency what the provider lets
  you do. A call needs room in both: a user with quota left gets what the app has left.
- **Kept a while:** the limits read are kept in each process for `settings_ttl` seconds (30): an
  admin panel that changes them in the database is obeyed by every process within that time, and
  at once by the one that calls `invalidate(user_id)` (`invalidate()` for everybody,
  `invalidate(total=True)` for the app's). While the database can't be read, the limits read
  last are kept; for a user never read, calls get a 503.
- **The units are yours:** tokens, messages, pages, or money in small units (thousandths of a
  cent) for one budget across providers. Whole numbers.

## In your database

`SQLBudgetLimits` reads them from a table of yours, and writes them for the admin panel
(`pip install "fastapi-alcazaba[sqlalchemy]"`):

```python
from fastapi_alcazaba.accounting import SQLBudgetLimits, provider_budgets_table

provider_budgets = provider_budgets_table(metadata)  # your migrations create it
limits = SQLBudgetLimits(sessions, provider_budgets, "openai", plan=plan_of)

openai = FastAPIProviderBudget("openai", subject=current_user_id, limits=limits, total=limits.total)

# The admin panel:
await limits.save(BudgetLimits(quota=5_000_000, rate=60), plan="pro")
await limits.save(BudgetLimits(quota=100_000), scope="default")
await limits.save(BudgetLimits(quota=50_000_000), scope="total")
await limits.save(BudgetLimits(enabled=False), user="u-123")  # cut off
openai.invalidate("u-123")
await limits.delete(user="u-123")  # back to the plan's
```

- **One row per scope:** a user's, a plan's (`plan=` gives a user's plan, plain or async), every
  user's (`default`) and the app's (`total`), per provider. A user gets the most specific one:
  theirs, else their plan's, else the default. A row holds all of its limits: an empty column is
  no limit, not "as the plan's".
- **Sessions:** a `sessionmaker` (run in a thread) or an `async_sessionmaker`, as the
  [outbox's](background.md).

## What they spent

```python
usage = await openai.usage(user_id)  # .used, .limit, .remaining, .period, .resets
app = await openai.usage(None)  # the whole app's
await openai.reset(user_id)  # an admin gives them their quota back for this period
```

- **Records:** each call that used something sends a `UsageRecord` (its `meter` is the provider)
  to the `sinks`, as [accounting's](accounting.md#usage-and-records) do: what you reconcile with
  the provider's invoice.
- **Metrics:** `fastapi_alcazaba.budget` counts the calls by provider and outcome, and
  `fastapi_alcazaba.budget.usage` the units used.
- **Keys:** the counters' keys are SHA-256 digests of the provider and the user's ID: the IDs
  never reach the backend in clear.

## When something fails

- **The backend:** the calls are refused with a 503 (`fail_open=False`, the default): going past
  a provider's budget costs money. `fail_open=True` makes them uncounted instead; the records
  still have them.
- **A process that dies during a call** keeps its reservation spent until the period ends: a
  budget never goes past its quota, and may fall short of it by what was reserved then.
- **The provider:** call it with [`ResilientTransport`](webhooks.md#outbound-calls) (a deadline and a circuit
  breaker): a call that fails before using anything gives its reservation back.
