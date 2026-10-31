<p align="center">
  <img src="https://raw.githubusercontent.com/nonodev96/fastapi-alcazaba/main/docs/assets/logo.png" alt="Alcazaba's logo: a fortress wall whose horseshoe-arch gate opens on a bolt" width="150">
</p>

<h1 align="center">Alcazaba</h1>

<p align="center">
  <strong>Secure, maintained tools for FastAPI</strong><br>
  Response cache, rate limits, sessions, CSRF, webhooks, a task queue and more, in one package.
</p>

<p align="center">
  <a href="https://github.com/nonodev96/fastapi-alcazaba/actions/workflows/ci.yml"><img src="https://github.com/nonodev96/fastapi-alcazaba/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/fastapi-alcazaba/"><img src="https://img.shields.io/pypi/v/fastapi-alcazaba?label=PyPI&color=c2552f" alt="PyPI version"></a>
  <a href="https://pypi.org/project/fastapi-alcazaba/"><img src="https://img.shields.io/pypi/pyversions/fastapi-alcazaba?label=Python&color=1b2e42" alt="Python versions"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/nonodev96/fastapi-alcazaba"><img src="https://api.scorecard.dev/projects/github.com/nonodev96/fastapi-alcazaba/badge" alt="OpenSSF Scorecard"></a>
  <a href="https://github.com/nonodev96/fastapi-alcazaba/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-0e9e8e" alt="License: Apache 2.0"></a>
</p>

---

**Documentation**: <https://nonodev96.github.io/fastapi-alcazaba/>

**Source code**: <https://github.com/nonodev96/fastapi-alcazaba>

---

Alcazaba (`fastapi-alcazaba`) gathers the pieces most FastAPI apps end up writing themselves: a
response cache, rate limits, authorization, sessions, CSRF protection, webhooks, a task queue and
more, built with security in mind.

The key features are:

- **Secure by default:** users never get each other's cached responses, nothing that opens a
  door (keys, codes, tokens) is stored in clear, and whatever could leak data is opt-in.
- **Shared by every process:** rate limits, locks, sessions, revoked tokens, feature flags and
  maintenance mode live in Redis, Valkey, memcached or DynamoDB, so that every process and
  container sees them at once.
- **Resilient:** every call to the backend has a timeout and a circuit breaker. A backend that
  fails doesn't take the app down.
- **The FastAPI way:** dependencies, decorators and middlewares that show in the OpenAPI docs,
  with types your editor completes.
- **Standards:** problem details (RFC 9457), `Cache-Status` (RFC 9211), the IETF's `RateLimit`
  headers, Standard Webhooks, Casbin policies and OpenTelemetry.
- **Tested hard:** against real servers on Python 3.11 to 3.14, with fuzzing and mutation
  testing.
- **Many tools, one package:** a response cache, rate and concurrency limits, idempotency keys,
  authentication strategies, API keys and tokens, authorization, sessions and CSRF, an audit
  trail, upload checks, webhooks, scheduled jobs, a task queue, circuit breakers, maintenance
  mode and feature flags. [See them all](features/tools.md).

[Get started](guide/index.md){ .md-button .md-button--primary }
[Features](features/index.md){ .md-button }

## Requirements

Python 3.11 or later, FastAPI 0.115 or later and Pydantic v2. Everything else is optional: each
backend and integration comes with an [extra](#optional-dependencies).

## Installation

```console
$ pip install fastapi-alcazaba
```

That's enough for an app in a single process, with everything kept in memory.

## Example

### Create it

Create a file `main.py` with:

<!-- fmt: off -->
```python title="main.py"
--8<-- "examples/quickstart/step1_cache.py"
```
<!-- fmt: on -->

### Run it

Run it with `fastapi dev`, which comes with `pip install "fastapi[standard]"` (or
`uvicorn main:app --reload`):

```console
$ fastapi dev main.py

 ⚡️ Starting FastAPI in development mode

 🌐 Server started at http://127.0.0.1:8000
    Documentation at http://127.0.0.1:8000/docs
```

### Check it

Ask for the product twice:

```console
$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60

{"name":"Olive oil","price":9.5}

$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
cache-status: Alcazaba; hit; ttl=60
cache-control: max-age=60

{"name":"Olive oil","price":9.5}
```

The first request ran `read_product`; the second one came from the cache, which keeps the
response for 60 seconds. Requests with an `Authorization` or `Cookie` header get entries of
their own: users never see each other's responses.

### Interactive API docs

Open <http://127.0.0.1:8000/docs>: the endpoint is there as FastAPI documents it, with its
parameters and response model.

### Example upgrade

The [Tutorial - User Guide](guide/index.md) takes this app further, a step at a time: five
requests a minute for each client, then prices that only the staff may change, as a Casbin policy
says:

<!-- fmt: off -->
```python
--8<-- "examples/quickstart/step3_authz.py:58:64"
```
<!-- fmt: on -->

### Recap

In summary, you add Alcazaba's tools the way FastAPI already works: a decorator caches an
endpoint, a dependency limits it or checks who may call it, and the lifespan sets up where their
state lives. The app keeps its OpenAPI schema and its types, and moving from memory to Redis
changes one line.

For a more complete example, with more tools, see the [Tutorial - User Guide](guide/index.md).

## Optional dependencies

Each backend and integration needs a library of its own, which an extra installs:

| Extra | Installs | For |
| --- | --- | --- |
| `redis` | redis-py | `RedisBackend` |
| `valkey` | valkey-py | `ValkeyBackend` |
| `glide` | Valkey GLIDE | `GlideBackend` |
| `memcache` | aiomcache | `MemcachedBackend` |
| `dynamodb` | aiobotocore | `DynamoBackend` |
| `otel` | the OpenTelemetry API | [metrics and traces](guide/observability.md#metrics-and-traces) |
| `crypto` | cryptography | [encrypted entries](guide/cache.md#encrypted-entries) |
| `zstd` | backports.zstd, before Python 3.14 | [zstd compression](guide/cache.md#compression) |
| `casbin` | PyCasbin | the enforcers of [`FastAPICasbin`](guide/authz.md) |
| `auth-strategy-jwt` | PyJWT, with cryptography | [`JWTStrategy`](guide/authentication/jwt.md) |
| `auth-strategy-oauth2` | httpx2 | [`OAuth2Strategy`](guide/authentication/oauth2.md) |
| `auth-strategy-saml` | signxml | [`SAMLStrategy`](guide/authentication/saml.md) |
| `auth-strategy-http-bearer` | nothing more | [`HTTPBearerStrategy`](guide/authentication/http-bearer.md) |
| `all` | all of the above | |

```console
$ pip install "fastapi-alcazaba[redis,casbin]"
```

## License

This project is licensed under the terms of the
[Apache License 2.0](https://github.com/nonodev96/fastapi-alcazaba/blob/main/LICENSE). Its
credits, and where its name comes from, are in [About](about/index.md#license-and-credits).
