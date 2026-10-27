"""Alcazaba with Redis (or Valkey), signed entries and per-user caching.

    export REDIS_URL=redis://localhost:6379/0
    export ALCAZABA_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    uvicorn examples.redis.main:app

The signing key comes from the environment, a .env file or a secret manager, never from the
code.
"""

import datetime
import itertools
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.response_cache import PickleCoder, cache


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    alcazaba_signing_key: SecretStr  # required: no key, no start


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    client = redis.from_url(settings.redis_url)
    FastAPICache.init(
        RedisBackend(client),
        prefix="fastapi-alcazaba-example",
        signing_key=settings.alcazaba_signing_key,
    )
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent)
calls = itertools.count(1)  # how many times get_ret() really ran


@cache(namespace="test", expire=1)
async def get_ret() -> int:
    return next(calls)


@app.get("/")
@cache(namespace="test", expire=10, vary_headers=())  # the same for everybody
async def index() -> dict[str, int]:
    return {"ret": await get_ret()}


@app.get("/clear")
async def clear() -> int:
    return await FastAPICache.clear(namespace="test")


def current_user(authorization: str | None = Header(default=None)) -> str:
    if authorization is None:
        raise HTTPException(status_code=401)
    return authorization.removeprefix("Bearer ")


@app.get("/me")
@cache(namespace="users", expire=30)  # one entry per Authorization header, sent as private
async def me(user: str = Depends(current_user)) -> dict[str, str]:
    return {"user": user}


@app.get("/date")
@cache(namespace="test", expire=10)
async def get_data(_request: Request, _response: Response) -> datetime.date:
    return datetime.date.today()


# Note: This function MUST be sync to demonstrate Alcazaba's correct handling,
# i.e. running cached sync functions in threadpool just like FastAPI itself!
@app.get("/blocking")
@cache(namespace="test", expire=10)
def blocking() -> dict[str, int]:
    time.sleep(2)
    return {"ret": 42}


@app.get("/datetime")
@cache(namespace="test", expire=2)
async def get_datetime(_request: Request, _response: Response) -> datetime.datetime:
    return datetime.datetime.now().astimezone()


# PickleCoder runs code when it reads an entry: only with a signing key, as above.
@app.get("/html", response_class=HTMLResponse)
@cache(expire=60, namespace="html", coder=PickleCoder, vary_headers=())
async def cache_html(request: Request) -> Response:
    return templates.TemplateResponse(request, "index.html", {"ret": await get_ret()})


@app.get("/cache_response_obj")
@cache(namespace="test", expire=5)
async def cache_response_obj() -> JSONResponse:
    return JSONResponse({"a": 1})


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
