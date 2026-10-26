"""An API with rate limits per client and per user, and a concurrency limit on its slowest
endpoint.

    uvicorn examples.limits.main:app

- `GET /search` is public: 10 requests a minute per client address (IPv6 addresses by their /64
  network, what an ISP gives a single customer).
- `GET /orders` and `POST /reports` are for signed-in users, and share one budget of 30 requests a
  minute per user, of which each report takes 10.
- `POST /reports` renders at most 2 reports at a time, across every process: a request waits up
  to 2 seconds for a slot, then gets a 503.

Over a rate limit, clients get a 429. Refused requests say when to retry (`Retry-After`), and
the other responses how much is left (`RateLimit` and `RateLimit-Policy`), for clients to slow
down before they get a 429.

The counters and the slots live in the cache's backend: InMemoryBackend here, so that it runs
without a server. Production uses Redis or Valkey, so that every process shares them.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from fastapi_alcazaba import Cache, FastAPIConcurrencyLimiter, FastAPIRateLimiter
from fastapi_alcazaba.backends.inmemory import InMemoryBackend

# Stands in for your users and their access tokens (a database, or a JWT check).
TOKENS = {"alice-token": "alice", "bob-token": "bob", "carol-token": "carol"}


class Product(BaseModel):
    name: str
    price: float


class Order(BaseModel):
    id: int
    customer: str
    total: float


class ReportRequest(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", examples=["2026-09"])


class Report(ReportRequest):
    customer: str
    pages: int


CATALOG = [
    Product(name="Olive oil", price=9.5),
    Product(name="Olives", price=3.2),
    Product(name="Oregano", price=2.1),
    Product(name="Orange jam", price=4.8),
]


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # InMemoryBackend counts in this process only. Production uses Redis or Valkey
    # (RedisBackend(redis.asyncio.from_url(...)), with the URL in the app's settings): with
    # several processes, each would otherwise allow its own 10 searches a minute and its own 2
    # reports at a time.
    Cache(InMemoryBackend(), prefix="limits").install(application)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/search", dependencies=[Depends(FastAPIRateLimiter(10, minutes=1))])
async def search(q: Annotated[str, Query(min_length=2, max_length=50)]) -> list[Product]:
    """Public: 10 requests a minute per client address. Behind a proxy, let uvicorn take the
    address from its headers (`--proxy-headers --forwarded-allow-ips=<proxy address>`): the
    limiter never reads `X-Forwarded-For`, which any client can set."""
    return [product for product in CATALOG if q.lower() in product.name.lower()]


def authenticate(request: Request) -> str:
    """Stands in for your authentication: the user whose bearer token it is, else a 401."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    user = TOKENS.get(token) if scheme.lower() == "bearer" else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    request.state.user_id = user
    return user


def user_id(connection: HTTPConnection) -> str:
    """Whose budget a request takes: the user that authenticate() found."""
    user: str = connection.state.user_id
    return user


User = Annotated[str, Depends(authenticate)]

# The router's dependencies run before the routes' own: user_id() finds the user, and requests
# without a valid token are refused before they take anybody's budget.
api = APIRouter(dependencies=[Depends(authenticate)])

# Limiters with the same name and rate share one budget, whatever the route; `cost=` makes a
# route take more of it.
api_budget = partial(FastAPIRateLimiter, 30, minutes=1, name="api", identifier=user_id)

# At most 2 reports at a time, whoever asks for them: each one keeps a CPU busy for seconds.
# Named, so that a job outside requests can take the same slots: `async with report_slots.slot()`.
report_slots = FastAPIConcurrencyLimiter(2, name="reports", wait=2, retry_after=3)


@api.get("/orders", dependencies=[Depends(api_budget())])
async def orders(user: User) -> list[Order]:
    return [Order(id=n, customer=user, total=12.5 * n) for n in range(1, 4)]


async def render_report(user: str, month: str) -> Report:
    """Stands in for the slow part: rendering a PDF, OCR, a call to an AI model."""
    await asyncio.sleep(3)
    return Report(customer=user, month=month, pages=12)


# The budget before the slots: a client over its budget is refused at once, instead of waiting
# for a slot first.
@api.post("/reports", dependencies=[Depends(api_budget(cost=10)), Depends(report_slots)])
async def create_report(report: ReportRequest, user: User) -> Report:
    """10 of the user's 30 a minute, and one of the 2 slots while it renders."""
    return await render_report(user, report.month)


app.include_router(api)


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
