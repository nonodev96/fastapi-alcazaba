# Production setup

The [quick start](index.md) keeps everything in the process: each process has its own cache,
counters and revoked tokens. In production, several processes or containers serve the app, and
they share that state through a backend: Redis, Valkey, memcached or DynamoDB.

## Install the extras

Each backend and integration needs a library of its own, which an extra installs:

```shell
pip install fastapi-alcazaba            # in-memory backend only
pip install "fastapi-alcazaba[redis]"   # or [valkey], [glide], [memcache], [dynamodb], [all]
pip install "fastapi-alcazaba[otel]"    # OpenTelemetry metrics
pip install "fastapi-alcazaba[crypto]"  # encrypted entries
pip install "fastapi-alcazaba[zstd]"    # zstd compression before Python 3.14
pip install "fastapi-alcazaba[casbin]"  # authorization with Casbin
pip install "fastapi-alcazaba[auth-strategy-jwt]"     # JWTStrategy
pip install "fastapi-alcazaba[auth-strategy-oauth2]"  # OAuth2Strategy
pip install "fastapi-alcazaba[auth-strategy-saml]"    # SAMLStrategy
pip install "fastapi-alcazaba[sqlalchemy]"            # pagination queries for SQLAlchemy
```

Python 3.11+, FastAPI 0.115+ and Pydantic v2.

## The app

An app as it runs in production: its configuration from the environment, the cache in Redis,
shared by every process, and its entries signed.

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis import asyncio as aioredis

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.response_cache import cache


class Settings(BaseSettings):
    """From environment variables (REDIS_URL...) or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    redis_url: str
    cache_signing_key: SecretStr  # secrets.token_urlsafe(32)


settings = Settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    redis = aioredis.from_url(settings.redis_url)
    FastAPICache.init(RedisBackend(redis), prefix="myapp", signing_key=settings.cache_signing_key)
    yield
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


class Product(BaseModel):
    name: str
    price: float


def current_user(authorization: str = Header()) -> str:
    return authorization  # validate the token here


@app.get("/products/{product_id}")
@cache(expire=60, vary_headers=())  # same response for everybody: one shared entry
async def product(product_id: int) -> Product:
    return Product(name="Olive oil", price=9.5)


@app.get("/me")
@cache(expire=30)  # one entry per Authorization/Cookie value, sent as private
async def me(user: str = Depends(current_user)) -> dict[str, str]:
    return {"user": user}
```

`@cache` goes between the route decorator and the function. It also caches plain functions,
sync or async (sync ones run in the thread pool, like FastAPI does).

The configuration comes from
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) (`pip install
pydantic-settings`). Every parameter that takes a key or a secret also takes pydantic's
`SecretStr` (or `SecretBytes`) as it is, so the value stays out of the settings' repr and of
tracebacks until Alcazaba uses it. The other snippets read theirs from the same `settings`.

## Next steps

- [Backends](backends.md): Redis, Valkey, GLIDE, memcached, DynamoDB, and a copy in each process
  in front of them.
- [Security model and resilience](security-model.md): what gets cached and for whom, what the
  backend is trusted with, and what happens when it fails.
- [Metrics and traces](observability.md#metrics-and-traces): what Alcazaba records with
  OpenTelemetry, and the logs to configure.
