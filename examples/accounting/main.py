"""An API that meters what each tenant uses, holds it to its plan, and records every charge for
billing.

    uvicorn examples.accounting.main:app

Each tenant has an API key and a plan. The free plan gets 3 searches a day and 50 tokens of
summaries a month; the pro plan, searches without a limit and 10,000 tokens a month.

- `GET /search` takes one search of the day; over the plan, a 429 until the day ends.
- `POST /summaries` takes the tokens it used (a word, a token here), counted when it answers;
  over the plan, a 402: a plan to upgrade.
- `GET /usage` tells the tenant what it has used and has left this period.

Every charge is a usage record, logged as a line of JSON by `fastapi_alcazaba.accounting.usage`:
what billing adds up. The counters live in the cache's backend: InMemoryBackend here, so that
it runs without a server. Production uses Redis or Valkey, so that every process shares them.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from fastapi_alcazaba import Cache
from fastapi_alcazaba.accounting import FastAPIAccounting, Metering, Quota
from fastapi_alcazaba.backends.inmemory import InMemoryBackend

Plan = Literal["free", "pro"]
# Stands in for your tenants and their API keys (a database, with the keys stored as digests).
TENANTS: dict[str, tuple[str, Plan]] = {
    "acme-key": ("acme", "free"),
    "globex-key": ("globex", "pro"),
}
PLAN_OF = dict(TENANTS.values())
#: What each plan allows: None for no limit.
SEARCHES: dict[Plan, int | None] = {"free": 3, "pro": None}
TOKENS: dict[Plan, int] = {"free": 50, "pro": 10_000}


class Summary(BaseModel):
    summary: str
    tokens: int


class Text(BaseModel):
    text: str = Field(max_length=10_000)


class Meter(BaseModel):
    used: int
    limit: int | None
    remaining: int | None
    period: str
    resets: str


api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


async def current_tenant(key: Annotated[str | None, Security(api_key)]) -> str | None:
    """The tenant of the request's API key; None without one (a 401)."""
    if key is None:
        return None
    if key not in TENANTS:
        raise HTTPException(401, "Invalid API key")
    return TENANTS[key][0]


def searches_of(tenant: str) -> int | None:
    return SEARCHES[PLAN_OF[tenant]]


async def tokens_of(tenant: str) -> int:
    # A coroutine function: where the plan comes from your database.
    return TOKENS[PLAN_OF[tenant]]


accounting = FastAPIAccounting(
    subject=current_tenant,
    quotas=[
        Quota("searches", limit=searches_of, period="day"),
        Quota("tokens", limit=tokens_of, status=402),  # a month
    ],
    challenge="ApiKey",
)


# The usage records, to the console: a log pipeline would keep them for billing.
records = logging.getLogger("fastapi_alcazaba.accounting.usage")
records.setLevel(logging.INFO)
if not records.handlers:
    records.addHandler(logging.StreamHandler())


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    Cache(InMemoryBackend(), prefix="accounting").install(application)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/search", dependencies=[Depends(accounting.meter("searches"))])
async def search(q: str) -> list[str]:
    """One search of the day, charged before it runs."""
    return [name for name in ("Olive oil", "Olives", "Oregano") if q.lower() in name.lower()]


@app.post("/summaries")
async def summarize(
    text: Text,
    usage: Annotated[Metering, Depends(accounting.meter("tokens", amount=None))],
) -> Summary:
    """The tokens it uses are charged when it answers: only then are they known."""
    words = text.text.split()
    summary = " ".join(words[:12])
    usage.add(len(words) + len(summary.split()))  # what it read and what it wrote
    return Summary(summary=summary, tokens=usage.total)


@app.get("/usage")
async def read_usage(
    request: Request,
    tenant: Annotated[str | None, Depends(current_tenant)],
) -> dict[str, Meter]:
    """What the tenant has used of each meter this period, and what its plan leaves."""
    if tenant is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "ApiKey"})
    meters = {}
    for meter in ("searches", "tokens"):
        report = await accounting.usage(tenant, meter, request=request)  # the app's cache
        meters[meter] = Meter(
            used=report.used,
            limit=report.limit,
            remaining=report.remaining,
            period=report.period,
            resets=report.resets.isoformat(),
        )
    return meters


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
