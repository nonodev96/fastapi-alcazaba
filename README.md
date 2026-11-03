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
| [Authentication](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/authentication/) | `auth` | `FastAPIAuth` with strategies: JWTs (signing keys of any type that rotate, refresh tokens), bearer tokens, sessions, OAuth2, OpenID Connect and SAML logins, Unkey's API keys, your own |
| [Tokens, codes and API keys](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/auth/) | `auth` | a JWT blocklist, refresh tokens that detect reuse, one-time codes and API keys, nothing stored in clear |
| [Passwords](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/passwords/) | `auth` | scrypt hashes with a pepper, leaked passwords refused, guesses limited per account, resets by a link that works once |
| [Passkeys](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/passkeys/) | `auth` | logins without a password (WebAuthn), which a phishing page can't relay |
| [Second factor](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/two-factor/) | `auth` | authenticator apps' codes (TOTP) after the password, recovery codes, step-up for sensitive actions |
| [Authorization](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/authorization/) | `authz` | `FastAPIAuthz`: a Casbin, Cedar or Open Policy Agent policy decides who may do what, per route or for every request |
| [Browser apps](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/browser/) | `security` | server-side sessions (each user's, listed and ended), CSRF protection, security headers and body size |
| [Webhooks and outbound calls](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/webhooks/) | `webhooks`, `outbound` | Standard Webhooks signatures; deadlines, breakers and SSRF protection for httpx |
| [Uploads](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/uploads/) | `uploads` | the real type from the bytes, size, image dimensions, safe names |
| [Object storage](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/storage/) | `uploads` | signed URLs and upload forms for S3, R2 or MinIO, without an SDK |
| [E-mail](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/mail/) | `mail` | password resets, codes and receipts over SMTP with TLS, without forged headers |
| [Pagination](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/pagination/) | `pagination` | page numbers, offsets, ranges, cursors, keysets, scrolls and streams, cached page by page |
| [Background work](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/background/) | `background` | a task queue (timeouts, unique tasks, status, a command line), scheduled jobs, locks, leader election, broadcast to every process and an outbox (purged, traced, recoverable) |
| [Operations](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/operations/) | `operations` | circuit breakers, maintenance mode and feature flags |
| [Observability](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/observability/) | `observability`, `telemetry` | problem details, request IDs, timeouts, health checks (of the queue, broadcast, leader, SMTP, storage), backlog gauges, OpenTelemetry, logs as JSON |
| [Accounting](https://nonodev96.github.io/fastapi-alcazaba/latest/guide/accounting/) | `accounting` | what each user, API key or tenant uses per period, held to its plan's quotas, a record per charge for billing |
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
