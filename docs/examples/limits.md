# Rate and concurrency limits

An API with a public search limited per client address, routes for signed-in users that share
one budget per user (a report takes ten times as much of it as a list of orders), and a slow
report endpoint that renders at most two reports at a time across every process.
[`FastAPIRateLimiter`](../guide/limits.md#rate-limiting) counts the requests per minute, and
[`FastAPIConcurrencyLimiter`](../guide/limits.md#concurrency-limits) the ones in flight.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.limits.main:app
```

The example has three users, `alice`, `bob` and `carol`, whose tokens are `alice-token`,
`bob-token` and `carol-token`.

## Try it

The public search allows 10 requests a minute per address. Every response says how many are left
(`r`), and for how many seconds at least (`t`):

```console
$ curl -i "http://127.0.0.1:8000/search?q=oli"
HTTP/1.1 200 OK
ratelimit: "10/60s";r=9;t=16
ratelimit-policy: "10/60s";q=10;w=60

[{"name":"Olive oil","price":9.5},{"name":"Olives","price":3.2}]
```

The eleventh within the minute is refused, with the seconds to wait:

```console
$ for i in $(seq 9); do curl -s -o /dev/null "http://127.0.0.1:8000/search?q=oli"; done
$ curl -i "http://127.0.0.1:8000/search?q=oli"
HTTP/1.1 429 Too Many Requests
ratelimit: "10/60s";r=0;t=22
ratelimit-policy: "10/60s";q=10;w=60
retry-after: 22

{"detail":"Too Many Requests"}
```

Signed-in users have one budget of 30 a minute for `/orders` and `/reports` together:

```console
$ curl -i http://127.0.0.1:8000/orders -H "Authorization: Bearer alice-token"
HTTP/1.1 200 OK
ratelimit: "api";r=29;t=16
ratelimit-policy: "api";q=30;w=60

[{"id":1,"customer":"alice","total":12.5},{"id":2,"customer":"alice","total":25.0},{"id":3,"customer":"alice","total":37.5}]
```

A report takes 10 of it, and three seconds to render:

```console
$ curl -i -X POST http://127.0.0.1:8000/reports -H "Authorization: Bearer alice-token" \
    -H "Content-Type: application/json" -d '{"month": "2026-09"}'
HTTP/1.1 200 OK
ratelimit: "api";r=19;t=16
ratelimit-policy: "api";q=30;w=60

{"month":"2026-09","customer":"alice","pages":12}
```

After a second report Alice has 9 left: not enough for a third, but enough to list her orders.
Bob's budget is his own:

```console
$ curl -si -X POST http://127.0.0.1:8000/reports -H "Authorization: Bearer alice-token" \
    -H "Content-Type: application/json" -d '{"month": "2026-10"}' | grep ratelimit:
ratelimit: "api";r=9;t=13

$ curl -i -X POST http://127.0.0.1:8000/reports -H "Authorization: Bearer alice-token" \
    -H "Content-Type: application/json" -d '{"month": "2026-08"}'
HTTP/1.1 429 Too Many Requests
ratelimit: "api";r=9;t=10
ratelimit-policy: "api";q=30;w=60
retry-after: 13

{"detail":"Too Many Requests"}

$ curl -si http://127.0.0.1:8000/orders -H "Authorization: Bearer alice-token" | grep ratelimit:
ratelimit: "api";r=8;t=10

$ curl -si http://127.0.0.1:8000/orders -H "Authorization: Bearer bob-token" | grep ratelimit:
ratelimit: "api";r=29;t=10
```

Without a valid token, a request is refused before it reaches any limiter:

```console
$ curl -i http://127.0.0.1:8000/orders
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Unauthorized"}
```

Three reports at once: two render, and the third waits 2 seconds for a slot, then gets a 503 with
`Retry-After: 3`:

```console
$ for month in 2026-07 2026-08 2026-09; do
    curl -s -o /dev/null -w "$month: %{http_code} after %{time_total}s\n" \
      -X POST http://127.0.0.1:8000/reports -H "Authorization: Bearer carol-token" \
      -H "Content-Type: application/json" -d "{\"month\": \"$month\"}" &
  done; wait
2026-08: 503 after 2.009854s
2026-09: 200 after 3.009745s
2026-07: 200 after 3.009700s
```

## What to look at

- **Who a client is:** `/search` counts by the address that the ASGI server reports (IPv6
  addresses by their /64 network), never by `X-Forwarded-For`. uvicorn takes the address from
  that header only on connections from `--forwarded-allow-ips` (`127.0.0.1` and `::1` by
  default, which is why a curl on the same machine can set it): set it to your proxy's address.
- **Authentication first:** `authenticate` is a dependency of the router, so it runs before the
  routes' own dependencies. The limiter's `identifier` reads the user it found, and a request
  without a valid token gets its 401 before it takes anybody's budget.
- **One budget, several routes:** the limiters made with `api_budget` have the same `name` and
  rate, so they share each user's 30 a minute; `cost=10` makes a report take ten of them, and a
  user with 9 left can still list orders. Requests that fail validation count too: FastAPI runs
  the dependencies before it answers 422.
- **The budget before the slots:** on `/reports` the rate limiter comes first, so a client over its
  budget is refused at once instead of waiting for a slot.
- **Slots for the slow part:** the slot is held while the report renders and given back after the
  response is sent. `name="reports"` lets a job outside requests take the same slots with
  `async with report_slots.slot()`.
- **Shared state:** InMemoryBackend counts in one process. With Redis or Valkey every process
  shares the counters and slots; otherwise each one would allow 10 searches a minute and 2 reports
  at a time of its own.

## The code

<!-- fmt: off -->
```python title="examples/limits/main.py"
--8<-- "examples/limits/main.py"
```
<!-- fmt: on -->
