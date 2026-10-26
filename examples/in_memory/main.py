import datetime
import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.decorator import cache


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    FastAPICache.init(InMemoryBackend())
    yield


app = FastAPI(lifespan=lifespan)

calls = itertools.count(1)  # how many times get_ret() really ran


@cache(namespace="test", expire=1)
async def get_ret():
    return next(calls)


@app.get("/")
@cache(namespace="test", expire=10)
async def index():
    return {"ret": await get_ret()}


@app.get("/clear")
async def clear():
    return await FastAPICache.clear(namespace="test")


@app.get("/date")
@cache(namespace="test", expire=10)
async def get_date():
    return datetime.date.today()


@app.get("/datetime")
@cache(namespace="test", expire=2)
async def get_datetime(_request: Request, _response: Response):
    return {"now": datetime.datetime.now().astimezone()}


@cache(namespace="test")
async def func_kwargs(*unused_args, **kwargs):
    return kwargs


@app.get("/kwargs")
async def get_kwargs(name: str):
    return await func_kwargs(name, name=name)


@app.get("/sync-me")
@cache(namespace="test")
def sync_me():
    # as per the fastapi docs, this sync function is wrapped in a thread,
    # thereby converted to async. Alcazaba does the same.
    return 42


@app.get("/cache_response_obj")
@cache(namespace="test", expire=5)
async def cache_response_obj():
    return JSONResponse({"a": 1})


class SomeClass:
    def __init__(self, value):
        self.value = value

    async def handler_method(self):
        return self.value


# register an instance method as a handler
instance = SomeClass(17)
app.get("/method")(cache(namespace="test")(instance.handler_method))


# cache a Pydantic model instance; the return type annotation is required in this case
class Item(BaseModel):
    name: str
    description: str | None = None
    price: float
    tax: float | None = None


@app.get("/pydantic_instance")
@cache(namespace="test", expire=5)
async def pydantic_instance() -> Item:
    return Item(name="Something", description="An instance of a Pydantic model", price=10.5)


puts = itertools.count(1)


@app.put("/uncached_put")
@cache(namespace="test", expire=5)
async def uncached_put():
    return {"value": next(puts)}


cached_puts = itertools.count(1)


@app.get("/cached_put")
@cache(namespace="test", expire=5)
async def cached_put():
    return {"value": next(cached_puts)}


@app.get("/namespaced_injection")
@cache(namespace="test", expire=5, injected_dependency_namespace="monty_python")
def namespaced_injection(
    __fastapi_alcazaba_request: int = 42,
    __fastapi_alcazaba_response: int = 17,
) -> dict[str, int]:
    return {
        "__fastapi_alcazaba_request": __fastapi_alcazaba_request,
        "__fastapi_alcazaba_response": __fastapi_alcazaba_response,
    }


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
