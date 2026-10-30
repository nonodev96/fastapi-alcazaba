"""An export of orders by scroll: a script reads every order in batches while the shop keeps
selling, and gets what was there when it started.

    uvicorn examples.pagination.scroll:app

`PaginationScroll` keeps what the query found as a snapshot in the cache's backend, and each
`scroll_id` reads its next batch: orders placed meanwhile don't show, none is skipped or seen
twice, and a retried request gets the same batch. Set SCROLL_SECRET and CACHE_SIGNING_KEY
(secrets.token_urlsafe(32) each), the same in every process, in the environment or a .env file.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Query, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.scroll import PaginationScroll, ScrollPage, ScrollParams


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    scroll_secret: SecretStr = Field(default_factory=throwaway_secret)
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class Status(StrEnum):
    PAID = "paid"
    SHIPPED = "shipped"


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=100)
    amount: float = Field(gt=0)


class Order(NewOrder):
    id: int
    status: Status = Status.PAID


class Database:
    """Stands in for the database: 12 orders, every third one shipped."""

    def __init__(self) -> None:
        self.orders = [
            Order(
                id=n,
                customer=f"customer-{n}",
                amount=10.0 * n,
                status=Status.SHIPPED if n % 3 == 0 else Status.PAID,
            )
            for n in range(1, 13)
        ]

    def find(self, state: Status | None, limit: int) -> list[Order]:
        """SELECT * FROM orders WHERE status = :state ORDER BY id LIMIT :limit"""
        return [order for order in self.orders if state is None or order.status == state][:limit]

    def add(self, order: NewOrder) -> Order:
        saved = Order(id=len(self.orders) + 1, **order.model_dump())
        self.orders.append(saved)
        return saved


db = Database()
# Signed: clients can't make scroll ids up. Every process must have the same secret, or a
# scroll_id made by one is refused by the others.
scroll = PaginationScroll(
    Order,
    settings.scroll_secret,
    default_batch_size=5,
    max_batch_size=50,
    keep_alive=60,
    max_items=1_000,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process reads the same snapshots. The
    # signing key seals them: whoever can write to the backend can't plant one.
    FastAPICache.init(InMemoryBackend(), prefix="shop", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/orders/export")
async def export_orders(
    params: Annotated[ScrollParams, Depends(scroll)],
    state: Annotated[Status | None, Query(alias="status")] = None,
) -> ScrollPage[Order]:
    """Without a scroll_id, the query runs and its results are kept for the next batches; with
    one, the next batch comes from them and the query doesn't run again. Not cached: each start
    keeps a snapshot of its own."""
    if params.scroll_id is None:
        # One more than the scroll keeps: if they're all there, start() refuses them.
        return await scroll.start(db.find(state, scroll.max_items + 1), params)
    return await scroll.next(params)


@app.delete("/orders/export", status_code=status.HTTP_204_NO_CONTENT)
async def stop_export(scroll_id: str) -> None:
    """The script is done (or gives up): the snapshot goes now, not when it expires."""
    await scroll.clear(scroll_id)


@app.post("/orders", status_code=status.HTTP_201_CREATED)
async def add_order(order: NewOrder) -> Order:
    """A new order: the exports running don't see it, the next ones will."""
    return db.add(order)


if __name__ == "__main__":
    uvicorn.run("scroll:app", reload=True)
