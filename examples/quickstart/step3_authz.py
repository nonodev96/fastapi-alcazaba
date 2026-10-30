from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import casbin
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache, FastAPICasbin, FastAPIRateLimiter
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
TOKENS = {"ana-token": "ana", "ben-token": "ben"}  # stands in for your authentication


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> str:
    if credentials.credentials not in TOKENS:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    return TOKENS[credentials.credentials]


here = Path(__file__).parent
enforcer = casbin.Enforcer(str(here / "model.conf"), str(here / "policy.csv"))
authz = FastAPICasbin(enforcer, subject=current_user)


@app.get(
    "/products/{product_id}",
    dependencies=[Depends(FastAPIRateLimiter(times=5, minutes=1))],
)
@cache(expire=60, tags=lambda product_id, **_: [f"product:{product_id}"])
async def read_product(product_id: int) -> Product:
    if product_id not in PRODUCTS:
        raise HTTPException(404, "No such product")
    return PRODUCTS[product_id]


@app.put("/products/{product_id}", dependencies=[Depends(authz)])
async def update_product(product_id: int, product: Product) -> Product:
    if product_id not in PRODUCTS:
        raise HTTPException(404, "No such product")
    PRODUCTS[product_id] = product
    await FastAPICache.invalidate_tags(f"product:{product_id}")
    return product
