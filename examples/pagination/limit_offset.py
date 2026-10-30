"""A price list for partners by limit and offset: any slice of the products, with the total.

    uvicorn examples.pagination.limit_offset:app

`PaginationLimitOffset` reads `limit` and `offset` from the query (a 422 for a limit over 50),
the database counts the products and fetches only the slice, and the response links to the first,
previous, next and last slices, on the grid of `limit` from 0. Filters stay in the links.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.limit_offset import (
    LimitOffsetPage,
    LimitOffsetParams,
    PaginationLimitOffset,
)
from fastapi_alcazaba.response_cache import cache


class Category(StrEnum):
    OIL = "oil"
    FRUIT = "fruit"


class Product(BaseModel):
    sku: str
    category: Category
    price: Decimal


class Database:
    """Stands in for the database: 60 products, by SKU."""

    def __init__(self) -> None:
        self.products = [
            Product(
                sku=f"SKU-{n:03}",
                category=Category.OIL if n % 2 else Category.FRUIT,
                price=Decimal(n) / 4,
            )
            for n in range(1, 61)
        ]

    def count(self, category: Category | None) -> int:
        """SELECT count(*) FROM products WHERE category = :category"""
        return len(self._found(category))

    def slice(self, category: Category | None, limit: int, offset: int) -> list[Product]:
        """SELECT * FROM products WHERE ... ORDER BY sku LIMIT :limit OFFSET :offset"""
        return self._found(category)[offset : offset + limit]

    def _found(self, category: Category | None) -> list[Product]:
        return [p for p in self.products if category is None or p.category == category]


db = Database()
slices = PaginationLimitOffset(default_limit=20, max_limit=50)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the slices.
    FastAPICache.init(InMemoryBackend(), prefix="prices", vary_headers=())  # public data
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/prices")
@cache(expire=300)
async def prices(
    params: Annotated[LimitOffsetParams, Depends(slices)],
    category: Category | None = None,
) -> LimitOffsetPage[Product]:
    """One entry per slice and category."""
    rows = db.slice(category, params.limit, params.offset)
    return slices.paginate(rows, params, total=db.count(category))


if __name__ == "__main__":
    uvicorn.run("limit_offset:app", reload=True)
