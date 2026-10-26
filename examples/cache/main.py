"""A shop's catalogue and account pages, cached by Alcazaba.

    uvicorn examples.cache.main:app

With REDIS_URL (redis://localhost:6379/0) the entries live in Redis and every process shares
them; without it, in this process's memory. Either way they're signed: set CACHE_SIGNING_KEY
(secrets.token_urlsafe(32)), the same in every process.

- `/products/{product_id}`: the same for everybody, one shared entry per product, tagged so that
  an update drops it (and the lists it appears in) without knowing their keys.
- `/me`: depends on who asks. Requests with `Authorization` get entries of their own, sent as
  `Cache-Control: private`, so no user ever gets another's page.
- `/me/recommendations`: `get_or_set`, for what doesn't fit a decorator.
- `exchange_rate`: a plain function cached like an endpoint.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from fastapi_alcazaba import Cache, FastAPICache, get_cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.types import Backend
from redis import asyncio as aioredis


class Product(BaseModel):
    id: int
    name: str
    price: float


class Shop:
    """Stands in for the database, counting the queries that reach it."""

    def __init__(self) -> None:
        self.products = {
            1: Product(id=1, name="Olive oil", price=9.5),
            2: Product(id=2, name="Olives", price=3.2),
        }
        self.queries = 0

    def product(self, product_id: int) -> Product | None:
        self.queries += 1
        return self.products.get(product_id)

    def recommendations(self, _user: str) -> list[Product]:
        """The same for everybody here; a real shop would look at the user's history."""
        self.queries += 1
        return list(self.products.values())


shop = Shop()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    url = os.environ.get("REDIS_URL")
    client = aioredis.from_url(url) if url else None
    backend: Backend = RedisBackend(client) if client is not None else InMemoryBackend()
    FastAPICache.init(
        backend,
        prefix="shop",
        # Throwaway key for trying it out: a real one comes from the environment or a secret
        # manager, and is the same in every process.
        signing_key=os.environ.get("CACHE_SIGNING_KEY", "development-only-signing-key-0123456"),
    )
    yield
    if client is not None:
        await client.aclose()


app = FastAPI(lifespan=lifespan)


def current_user(authorization: Annotated[str | None, Header()] = None) -> str:
    """Stands in for real authentication: the token is the user's name."""
    if authorization is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ")


@app.get("/products/{product_id}")
@cache(expire=600, vary_headers=(), tags=lambda product_id, **_: [f"product:{product_id}"])
async def product(product_id: int) -> Product:
    """Public data: one entry per product, whoever asks (`vary_headers=()`)."""
    found = shop.product(product_id)
    if found is None:
        raise HTTPException(404, "No such product")  # errors are never cached
    return found


@app.put("/products/{product_id}")
async def update_product(product_id: int, update: Product) -> Product:
    shop.products[product_id] = update
    await FastAPICache.invalidate_tags(f"product:{product_id}")
    return update


@app.get("/me")
@cache(expire=30)
async def me(user: Annotated[str, Depends(current_user)]) -> dict[str, str]:
    """Personal data: an entry per Authorization header, never shared."""
    return {"user": user}


@app.get("/me/recommendations")
async def recommendations(
    user: Annotated[str, Depends(current_user)],
    app_cache: Annotated[Cache, Depends(get_cache)],
) -> list[Product]:
    async def load() -> list[Product]:  # annotated: the entry comes back as products
        return shop.recommendations(user)

    return await app_cache.get_or_set(f"recommendations:{user}", load, expire=300)


@cache(expire=3600)
async def exchange_rate(currency: str) -> float:
    """A slow call to another service, made at most once an hour per currency."""
    return {"USD": 1.08, "GBP": 0.86}.get(currency, 1.0)


@app.get("/products/{product_id}/price")
async def price(product_id: int, currency: str = "EUR") -> dict[str, float]:
    found = shop.product(product_id)
    if found is None:
        raise HTTPException(404, "No such product")
    return {"price": round(found.price * await exchange_rate(currency), 2)}


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
