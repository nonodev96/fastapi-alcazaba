"""App for the end-to-end suite, run by uvicorn with several worker processes.

Configured through the environment (see test_server.py). Computations are counted in Valkey
itself, so the test can tell how many times a value was really computed across processes.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import valkey.asyncio
from fastapi import Depends, FastAPI, Request

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.backends.valkey import ValkeyBackend
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.limiter import FastAPIRateLimiter

PREFIX = os.environ.get("ALCAZABA_E2E_PREFIX", "fastapi-alcazaba-e2e")
client: Any = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global client
    client = valkey.asyncio.from_url(  # type: ignore[no-untyped-call]
        os.environ["ALCAZABA_E2E_VALKEY_URL"], socket_connect_timeout=0.2, socket_timeout=0.5
    )
    # Copies in each worker's memory for 30 s: only pub/sub invalidation keeps the workers in step.
    FastAPICache.init(
        TieredBackend(ValkeyBackend(client), local_ttl=30, invalidation_channel=f"{PREFIX}:inv"),
        prefix=PREFIX,
        encryption_key=os.environ["ALCAZABA_E2E_ENCRYPTION_KEY"],
        distributed_lock=True,
        backend_timeout=0.3,
        circuit_breaker_failures=3,
    )
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan)


async def computed(name: str) -> int:
    """Counts a computation of `name`; returns how many there have been."""
    try:
        return int(await client.incr(f"{PREFIX}:computed:{name}"))
    except Exception:  # the fail-open test runs without a reachable Valkey
        return 0


@app.get("/health")
async def health() -> dict[str, int]:
    return {"pid": os.getpid()}


@app.get("/slow")
@cache(expire=60, vary_headers=(), namespace="slow")
async def slow(x: int) -> dict[str, int]:
    await computed(f"slow:{x}")
    await asyncio.sleep(0.3)
    return {"x": x, "pid": os.getpid()}


@app.get("/me")
@cache(expire=60, namespace="me")
async def me(request: Request) -> dict[str, str | None]:
    # The user only reaches the result through the request: not part of the default key.
    await computed("me")
    return {"user": request.headers.get("authorization")}


@app.get("/products/{product_id}")
@cache(expire=60, vary_headers=(), tags=lambda product_id, **_: [f"product:{product_id}"])
async def product(product_id: int) -> dict[str, int]:
    return {"id": product_id, "computation": await computed(f"product:{product_id}")}


@app.post("/products/{product_id}/invalidate")
async def invalidate(product_id: int) -> dict[str, int]:
    return {"deleted": await FastAPICache.invalidate_tags(f"product:{product_id}")}


@app.get("/limited/{item_id}", dependencies=[Depends(FastAPIRateLimiter(times=5, minutes=1))])
async def limited(item_id: int) -> dict[str, int]:
    return {"id": item_id, "pid": os.getpid()}
