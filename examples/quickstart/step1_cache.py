from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.response_cache import cache


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    FastAPICache.init(InMemoryBackend(), prefix="shop")
    yield


app = FastAPI(lifespan=lifespan)


class Product(BaseModel):
    name: str
    price: float


PRODUCTS = {1: Product(name="Olive oil", price=9.5)}


@app.get("/products/{product_id}")
@cache(expire=60)
async def read_product(product_id: int) -> Product:
    if product_id not in PRODUCTS:
        raise HTTPException(404, "No such product")
    return PRODUCTS[product_id]
