"""A shop's olive oils sorted by price, browsed with "Previous" and "Next" by keyset.

    uvicorn examples.pagination.keyset:app

`GET /products?limit=3` gives the cheapest three, `after` the key of the last one and `before`
the key of the first (null on the first page). `?after=<after>` gives the next page and
`?before=<before>` the one before: each is a query through an index on (price, id), as fast on
the last page as on the first, and products added or deleted meanwhile don't shift the pages.

Keys are signed: set KEYSET_SECRET (secrets.token_urlsafe(32)), the same in every process, in
the environment or a .env file.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.keyset import KeysetPage, KeysetParams, PaginationKeyset
from fastapi_alcazaba.response_cache import cache


class NewProduct(BaseModel):
    name: str
    price: float


class Product(NewProduct):
    id: int


def by_price(product: Product) -> tuple[float, int]:
    """The sort key: the price, and the id between products of the same price."""
    return product.price, product.id


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        oils = [
            ("Arbequina 500 ml", 6.5),
            ("Picual 1 l", 9.9),
            ("Hojiblanca 500 ml", 6.5),
            ("Cornicabra 250 ml", 4.2),
            ("Empeltre 500 ml", 7.8),
            ("Picual 500 ml", 5.9),
            ("Arbequina 250 ml", 3.9),
            ("Manzanilla 1 l", 11.5),
            ("Hojiblanca 1 l", 9.9),
            ("Cornicabra 500 ml", 7.2),
            ("Picual 5 l", 38.0),
            ("Empeltre 250 ml", 4.2),
        ]
        self.products = [
            Product(id=i, name=name, price=price) for i, (name, price) in enumerate(oils, start=1)
        ]
        self.queries = 0

    def page(self, params: KeysetParams) -> list[Product]:
        """What a database does, through an index on (price, id):
        WHERE (price, id) > :after ORDER BY price, id LIMIT :fetch; or for the page before,
        WHERE (price, id) < :before ORDER BY price DESC, id DESC LIMIT :fetch, nearest first."""
        self.queries += 1
        if isinstance(params.before, tuple):
            before = params.before
            found = [p for p in self.products if by_price(p) < before]
            return sorted(found, key=by_price, reverse=True)[: params.fetch]
        after = params.after
        found = [p for p in self.products if not isinstance(after, tuple) or by_price(p) > after]
        return sorted(found, key=by_price)[: params.fetch]

    def add(self, product: NewProduct) -> Product:
        saved = Product(id=len(self.products) + 1, **product.model_dump())
        self.products.append(saved)
        return saved


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the entries.
    FastAPICache.init(InMemoryBackend(), prefix="shop", vary_headers=())  # public data
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    keyset_secret: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()
# Signed: clients can't make keys up. Every process must have the same secret, or a key made by
# one is refused by the others.
keyset = PaginationKeyset(secret=settings.keyset_secret, default_limit=3, max_limit=50)


@app.get("/products")
@cache(expire=300, tags=["products"], exclude_params=["db"])
async def products(
    params: Annotated[KeysetParams, Depends(keyset)],
    db: DatabaseDep,
) -> KeysetPage[Product]:
    """Cheapest first. One entry per key and limit: the page parameters are part of the key, the
    session isn't."""
    return keyset.paginate(db.page(params), params, key=by_price, queried=True)


@app.post("/products", status_code=status.HTTP_201_CREATED)
async def add_product(product: NewProduct, db: DatabaseDep) -> Product:
    """The pages around the new product change: drop them all."""
    saved = db.add(product)
    await FastAPICache.invalidate_tags("products")
    return saved


if __name__ == "__main__":
    uvicorn.run("keyset:app", reload=True)
