<p align="center">
  <img src="https://raw.githubusercontent.com/nonodev96/fastapi-alcazaba/main/docs/assets/logo.png" alt="Alcazaba's logo: a fortress wall whose horseshoe-arch gate opens on a bolt" width="150">
</p>

<h1 align="center">Alcazaba</h1>

<p align="center">
  <strong>Secure, maintained tools for FastAPI</strong><br>
  Response cache, rate limits, sessions, CSRF, webhooks, a task queue and more, in one package.
</p>

<p align="center">
  <a href="https://nonodev96.github.io/fastapi-alcazaba/"><strong>Documentation</strong></a> |
  <a href="https://nonodev96.github.io/fastapi-alcazaba/latest/examples/">Examples</a> |
  <a href="https://pypi.org/project/fastapi-alcazaba/">PyPI</a> |
  <a href="https://github.com/nonodev96/fastapi-alcazaba/blob/main/CHANGELOG.md">Changelog</a> |
  <a href="https://github.com/nonodev96/fastapi-alcazaba/blob/main/SECURITY.md">Security policy</a>
</p>

---

Alcazaba (`fastapi-alcazaba`) gathers the pieces most FastAPI apps end up writing themselves,
built with security in mind and tested against real servers. They share one backend (memory,
Redis, Valkey, memcached or DynamoDB), one format of errors and one set of metrics.

| Tool | In `fastapi_alcazaba.` | What it does |
| --- | --- | --- |
| [Response cache](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/cache/) | `response_cache` | `@cache` for endpoints and functions, invalidated by tag, signed or encrypted |
| [Rate and concurrency limits](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/limits/) | `rate_limit` | a sliding window, and at most N requests at a time, shared by every process |
| [Idempotency keys](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/idempotency/) | `idempotency` | a retried POST gets the first response instead of running twice |
| [Authentication](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/authentication/) | `auth` | `FastAPIAuth` with strategies: JWTs, bearer tokens, sessions, OAuth2 and SAML logins, your own |
| [Tokens, codes and API keys](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/auth/) | `auth` | a JWT blocklist, one-time codes and API keys, nothing stored in clear |
| [Authorization](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/authz/) | `authz` | `FastAPICasbin`: a Casbin policy decides who may do what, per route or for every request |
| [Browser apps](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/browser/) | `security` | server-side sessions, CSRF protection, security headers and body size |
| [Webhooks and outbound calls](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/webhooks/) | `webhooks`, `outbound` | Standard Webhooks signatures; deadlines, breakers and SSRF protection for httpx |
| [Uploads](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/uploads/) | `uploads` | the real type from the bytes, size, image dimensions, safe names |
| [Pagination](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/pagination/) | `pagination` | page numbers, offsets, ranges, cursors, keysets, scrolls and streams, cached page by page |
| [Background work](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/background/) | `background` | a task queue, scheduled jobs, locks and leader election |
| [Operations](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/operations/) | `operations` | circuit breakers, maintenance mode and feature flags |
| [Observability](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/observability/) | `observability`, `telemetry` | problem details, request IDs, timeouts, health checks, OpenTelemetry |
| [Audit trail](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/audit/) | `audit` | who did what to which resource; no secrets, signed and chained |

## Install

```shell
pip install fastapi-alcazaba            # in-memory backend only
pip install "fastapi-alcazaba[redis]"   # or [valkey], [glide], [memcache], [dynamodb], [all]
```

Python 3.11+, FastAPI 0.115+ and Pydantic v2. The other extras (OpenTelemetry, encryption,
Casbin, JWT, OAuth2, SAML...) are in the
[guide](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/production/).

## A taste

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter
from fastapi_alcazaba.response_cache import cache


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    FastAPICache.init(InMemoryBackend(), prefix="shop")  # RedisBackend for several processes
    yield


app = FastAPI(lifespan=lifespan)


class Product(BaseModel):
    name: str
    price: float


@app.get(
    "/products/{product_id}",
    dependencies=[Depends(FastAPIRateLimiter(times=10, seconds=1))],  # per client
)
@cache(expire=60, vary_headers=())  # the same response for everybody: one shared entry
async def product(product_id: int) -> Product:
    return Product(name="Olive oil", price=9.5)
```

The [guide](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/) starts with an app built
in three steps, and each tool has a page of its own and an example app that runs as it is.

## Security

The security tools (revocations, idempotency, webhooks, sessions, the task queue) fail closed
when the backend does, and none of them puts secrets in logs, keys or errors. The
[security model](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/security-model/)
says what each tool protects against and what it needs from you; report vulnerabilities as
[SECURITY.md](SECURITY.md) says.

## Development

```shell
make deps               # .venv with every extra
make lint               # ruff, mypy and pylint
make test               # the unit tests, with coverage
make services-up        # Redis, Valkey, memcached and DynamoDB in Docker, for:
make test-integration
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the rest.

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE).
