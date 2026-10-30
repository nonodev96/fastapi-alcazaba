# Tutorial - User Guide

This tutorial shows you how to use Alcazaba, step by step. It starts on this page with a quick
start: a small shop API built in three steps, each adding one tool to the same `main.py` (a
response cache, a rate limit and authorization with Casbin), each run and checked. The next pages
take each tool in turn: read them in order, or go straight to the one you need.

!!! info "Before you start"

    You need Python 3.11 or later, and to know FastAPI's basics: path operations, dependencies and
    the lifespan. If they're new to you, FastAPI's own
    [tutorial](https://fastapi.tiangolo.com/tutorial/first-steps/) explains them first.

## Install

Make a folder for the project, with a virtual environment, and install FastAPI and Alcazaba:

=== "pip"

    ```console
    $ python -m venv .venv
    $ source .venv/bin/activate
    $ pip install "fastapi[standard]" "fastapi-alcazaba[casbin]"
    ```

=== "uv"

    ```console
    $ uv venv
    $ source .venv/bin/activate
    $ uv pip install "fastapi[standard]" "fastapi-alcazaba[casbin]"
    ```

`fastapi[standard]` brings the `fastapi` command and uvicorn, to run the app. Alcazaba itself
only needs FastAPI; the `casbin` extra is for the third step.

## Step 1: cache an endpoint

### Create it

Create a file `main.py` with:

<!-- fmt: off -->
```python title="main.py" hl_lines="7-9 14 30"
--8<-- "examples/quickstart/step1_cache.py"
```
<!-- fmt: on -->

### Run it

Run the development server:

```console
$ fastapi dev main.py

 ⚡️ Starting FastAPI in development mode

 🐍 Using import string: main:app

 🌐 Server started at http://127.0.0.1:8000
    Documentation at http://127.0.0.1:8000/docs

  Logs:

INFO:     Will watch for changes in these directories: ['/home/user/shop']
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [213542] using WatchFiles
INFO:     Started server process [213546]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

It reloads the app whenever you save `main.py`: leave it running through the next steps.

??? tip "With uvicorn"

    `fastapi dev` runs uvicorn for you. You can also run it yourself:

    ```console
    $ uvicorn main:app --reload
    ```

### Check it

In another terminal, ask for the product twice:

```console
$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
content-type: application/json
etag: W/"d5c916899a1291fccfef605e3d4ede40"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60
vary: Authorization, Cookie

{"name":"Olive oil","price":9.5}

$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
content-type: application/json
etag: W/"d5c916899a1291fccfef605e3d4ede40"
cache-status: Alcazaba; hit; ttl=60
cache-control: max-age=60
vary: Authorization, Cookie

{"name":"Olive oil","price":9.5}
```

The first request ran `read_product` and stored its response: `fwd=miss; stored`. The second one
came from the cache, `hit`, with the seconds the entry has left. A product that isn't there gets
its 404, which is never cached:

```console
$ curl http://127.0.0.1:8000/products/2
{"detail":"No such product"}
```

### Interactive API docs

Open <http://127.0.0.1:8000/docs>. FastAPI's interactive docs show the endpoint as usual: `@cache`
keeps its parameters and its response model. Try it out twice, and look at the `cache-status`
header of each response.

### Recap, step by step

**`FastAPICache.init()`**, in the lifespan, says where the entries live:

<!-- fmt: off -->
```python hl_lines="3"
--8<-- "examples/quickstart/step1_cache.py:12:15"
```
<!-- fmt: on -->

`InMemoryBackend` keeps them in the process, which is enough to try things out. In production,
[Redis or Valkey](backends.md) keeps them for every process at once.

**`@cache(expire=60)`** keeps each response for 60 seconds:

<!-- fmt: off -->
```python hl_lines="2"
--8<-- "examples/quickstart/step1_cache.py:29:34"
```
<!-- fmt: on -->

!!! warning "Under the route's decorator"

    `@cache` goes between `@app.get(...)` and the function. FastAPI registers the function
    below its decorator: with `@cache` above `@app.get`, the route would serve the uncached
    function.

!!! note "Technical details"

    Each user gets entries of their own: requests with an `Authorization` or `Cookie` header
    have their own keys, and their responses are sent as `Cache-Control: private`. That's what
    `vary: Authorization, Cookie` tells browsers and proxies. `Cache-Status` is the header of
    [RFC 9211](https://www.rfc-editor.org/rfc/rfc9211), and the `ETag` lets clients revalidate
    with `If-None-Match` and get a `304 Not Modified`. The [response cache](cache.md) page
    has the rest: shared entries, keys, signed entries.

## Step 2: limit the rate

Reading a product is cheap now, but nothing stops a single client from taking all the server's
time. Give each client five requests a minute:

<!-- fmt: off -->
```python title="main.py" hl_lines="4 7 29-32"
--8<-- "examples/quickstart/step2_limits.py"
```
<!-- fmt: on -->

### Check it

`fastapi dev` has reloaded the app. Make six requests:

```console
$ for i in 1 2 3 4 5 6; do curl -s -o /dev/null -w "%{http_code} " http://127.0.0.1:8000/products/1; done; echo
200 200 200 200 200 429

$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 429 Too Many Requests
ratelimit: "5/60s";r=0;t=15
ratelimit-policy: "5/60s";q=5;w=60
retry-after: 15
content-type: application/json

{"detail":"Too Many Requests"}
```

The sixth request and those after it get a `429 Too Many Requests`, with `Retry-After`: the
seconds to wait. Every response carries the `RateLimit` header, so that clients can slow down
before they get there: `r` is what's left, `t` the seconds until there's more.

### Recap

**`FastAPIRateLimiter`** is a FastAPI dependency: put it in `dependencies=[...]` of a route, of
a router, or of the whole app. Dependencies run before the endpoint, so cached responses count
too.

!!! tip "Behind a proxy"

    The limiter counts per client address, the one the ASGI server reports. Behind a proxy or a
    load balancer, tell uvicorn to take it from the proxy's headers
    (`--forwarded-allow-ips`); [Rate limiting](limits.md#rate-limiting) tells how, and how
    to count per user or per API key instead.

## Step 3: authorize with Casbin

Anybody may read the products, but only the staff may change their prices. Who's staff isn't
written in the code: it's in a [Casbin](https://casbin.org) policy. Create these two files next to
`main.py`:

=== "model.conf"

    ```ini title="model.conf"
    --8<-- "examples/quickstart/model.conf"
    ```

=== "policy.csv"

    ```text title="policy.csv"
    --8<-- "examples/quickstart/policy.csv"
    ```

The model says what a request is, (user, object, action), and when the policy allows it: when
one of the user's roles (`g`) has a rule (`p`) whose path pattern matches the request's
(`keyMatch2`, which takes `:id` for any value) and whose action is the request's. The policy
says that the staff may `PUT` any product, and that Ana is staff.

Then use it in `main.py`:

<!-- fmt: off -->
```python title="main.py" hl_lines="3-4 6 8 11 31 34-39 42-44 51 58-64"
--8<-- "examples/quickstart/step3_authz.py"
```
<!-- fmt: on -->

### Check it

Read the product, which caches it:

```console
$ curl http://127.0.0.1:8000/products/1
{"name":"Olive oil","price":9.5}
```

Then change its price without a token, as Ben, and as Ana:

```console
$ curl -i -X PUT http://127.0.0.1:8000/products/1 -H "Content-Type: application/json" \
    -d '{"name": "Olive oil", "price": 8.0}'
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}

$ curl -i -X PUT http://127.0.0.1:8000/products/1 -H "Authorization: Bearer ben-token" \
    -H "Content-Type: application/json" -d '{"name": "Olive oil", "price": 8.0}'
HTTP/1.1 403 Forbidden

{"detail":"Forbidden"}

$ curl -X PUT http://127.0.0.1:8000/products/1 -H "Authorization: Bearer ana-token" \
    -H "Content-Type: application/json" -d '{"name": "Olive oil", "price": 8.0}'
{"name":"Olive oil","price":8.0}
```

And the product has its new price at once, although its old one was cached:

```console
$ curl -i http://127.0.0.1:8000/products/1
HTTP/1.1 200 OK
content-type: application/json
ratelimit: "5/60s";r=3;t=2
ratelimit-policy: "5/60s";q=5;w=60
etag: W/"615d43a0044f76d19c5f354c5be3a495"
cache-status: Alcazaba; fwd=miss; stored
cache-control: max-age=60
vary: Authorization, Cookie

{"name":"Olive oil","price":8.0}
```

In the [interactive docs](http://127.0.0.1:8000/docs), the `PUT` endpoint now has a lock: click
**Authorize**, give `ana-token`, and try it out.

### Recap

**`FastAPICasbin`** takes the enforcer, which reads the model and the policy, and `subject`: a
dependency that says who makes the request. `current_user` stands in for your authentication
here; with yours, it decodes a JWT or reads a session.

<!-- fmt: off -->
```python hl_lines="3"
--8<-- "examples/quickstart/step3_authz.py:42:44"
```
<!-- fmt: on -->

**`Depends(authz)`** asks the policy about the request: may `ana` `PUT` `/products/1`? A
request without a token gets a 401 before the policy is asked; one that the policy refuses, a
403.

**Tags** drop cached entries when the data changes: `tags=` names the product's entry, and
`FastAPICache.invalidate_tags()` drops it after the update.

!!! tip "Your own actions"

    The path and the method aren't the only things to ask about: `authz.require("prices",
    "change")` asks whether the user may change prices, whatever the route.
    [Authorization with Casbin](authz.md#casbin-policies) tells how, and how to open routes
    to visitors without an account.

## Recap

You have an app whose product pages are cached and refreshed when they change, whose clients
can't flood it, and whose prices only the staff can change:

- `FastAPICache.init()` in the lifespan, and `@cache(...)` under the route's decorator.
- `FastAPIRateLimiter(...)` in a route's dependencies.
- `FastAPICasbin(enforcer, subject=...)` in a route's dependencies, with a Casbin model and
  policy.

!!! info "In production"

    So far everything lives in the process: each process has its own cache and counters. With
    Redis or Valkey every process shares them, and a signing key keeps whoever can write to the
    backend from planting entries: see the [production setup](production.md).

## Next steps

The next pages take each tool in turn, starting with the [response cache](cache.md). Each one
says what the tool does, its options and what it protects against, and links to an example app
and to the reference.

<div class="grid cards" markdown>

-   :material-cached:{ .lg .middle } __[Each tool](cache.md)__

    ---

    The cache, the backends, pagination, limits, authentication and authorization, sessions,
    webhooks, background work and operations.

-   :material-server-network:{ .lg .middle } __[Production setup](production.md)__

    ---

    The configuration from the environment, and the state in Redis, shared by every process.

-   :material-application-brackets:{ .lg .middle } __[Examples](../examples/index.md)__

    ---

    Complete apps, each with a tool at work as it's meant to be used in production.

-   :material-api:{ .lg .middle } __[Reference](../reference/index.md)__

    ---

    Every class and function, with its parameters.

</div>
