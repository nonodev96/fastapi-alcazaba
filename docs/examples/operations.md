# Operations

--8<-- "docs/examples/animations/operations.svg"

An app operated at runtime, without a deploy, through a small admin API that takes a bearer token.
It closes and reopens the app with [`FastAPIMaintenance`](../guide/operations.md#maintenance-mode), whose
middleware answers 503 with `Retry-After` to everything but the health check and the admin routes.
It defines [`FastAPIFeatureFlags`](../guide/operations.md#feature-flags): a new checkout rolled out to a
percentage of the users, and a beta page that only the users on an allow list can see (a 404 for
everybody else). And a [`FastAPIBreaker`](../guide/operations.md#circuit-breakers) turns the failures of a
flaky shipping service into a fast 503 while it's down.

The maintenance switch and the flags live in the backend, so a change made through one process
reaches all of them. Each process keeps a copy for up to 1 s (maintenance) or 5 s (flags), so
that requests don't each ask the backend. The breaker is each process's own.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.operations.main:app
```

The admin routes take `Authorization: Bearer $ADMIN_TOKEN`. Set `ADMIN_TOKEN` and
`CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)`), the same in every process. Without
`ADMIN_TOKEN`, the app makes one up at each start and logs it:

```console
ADMIN_TOKEN isn't set: the admin token of this run is YbLt_8-WKnm4zWknL3mgHNar4E3MmBsCNtk6KPJLBB4
```

It keeps the switch and the flags in memory, so don't start it with `--workers`: each worker would
have a switch of its own. In production, a Redis or Valkey backend lets every process share them.

## Try it

```console
$ TOKEN=YbLt_8-WKnm4zWknL3mgHNar4E3MmBsCNtk6KPJLBB4
```

Without the token, the admin routes answer 401:

```console
$ curl -i -X PUT http://127.0.0.1:8000/admin/maintenance -H "Content-Type: application/json" -d '{}'
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Unauthorized"}
```

Close the app for maintenance. Every request gets a 503 with the message and `Retry-After`, but
the health check is still served:

```console
$ curl -X PUT http://127.0.0.1:8000/admin/maintenance -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d '{"message": "Back at 10:30", "retry_after": 900}'
{"enabled":true,"message":"Back at 10:30","retry_after":900,"since":1793011967.4372241}

$ curl -i http://127.0.0.1:8000/checkout
HTTP/1.1 503 Service Unavailable
cache-control: no-store
retry-after: 900

{"detail": "Back at 10:30"}

$ curl http://127.0.0.1:8000/health
{"status":"ok"}

$ curl -i -X DELETE http://127.0.0.1:8000/admin/maintenance -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content

$ curl http://127.0.0.1:8000/checkout
{"checkout":"classic"}
```

Roll the new checkout out to 30 % of the users. Here the bearer token stands for the user, so that
you can try it with curl. Each user keeps their answer from one request to the next, and anonymous
visitors aren't part of a partial rollout:

```console
$ curl -X PUT http://127.0.0.1:8000/admin/flags/new-checkout -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d '{"enabled": true, "percentage": 30}'
{"name":"new-checkout","enabled":true,"percentage":30.0,"allow":[],"deny":[]}

$ for user in ana bea carlos david elena; do \
    echo "$user: $(curl -s http://127.0.0.1:8000/checkout -H "Authorization: Bearer $user")"; \
  done
ana: {"checkout":"new"}
bea: {"checkout":"classic"}
carlos: {"checkout":"classic"}
david: {"checkout":"new"}
elena: {"checkout":"classic"}

$ curl http://127.0.0.1:8000/checkout
{"checkout":"classic"}
```

The beta report is a 404, like a page that doesn't exist, until the user is on the allow list.
Without a percentage, a flag with an allow list is for nobody else. The list comes back as
digests: the backend never gets the users' ids.

```console
$ curl http://127.0.0.1:8000/reports/beta -H "Authorization: Bearer ana"
{"detail":"Not Found"}

$ curl -X PUT http://127.0.0.1:8000/admin/flags/beta-reports -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d '{"enabled": true, "allow": ["ana"]}'
{"name":"beta-reports","enabled":true,"percentage":null,"allow":["44d27c2ae32d479abf13f231ea9ee8295988d4a14fdf8be30c16aa81e894946b"],"deny":[]}

$ curl http://127.0.0.1:8000/reports/beta -H "Authorization: Bearer ana"
{"report":"Sales by hour"}

$ curl -i http://127.0.0.1:8000/reports/beta -H "Authorization: Bearer bea"
HTTP/1.1 404 Not Found

{"detail":"Not Found"}
```

Make the shipping service fail. The first three requests wait for it and get a 502; then the
breaker opens, and requests get a 503 at once, without calling the service:

```console
$ curl http://127.0.0.1:8000/orders/7/shipping
{"order_id":7,"carrier":"Alhambra Express","days":2}

$ curl -X PUT "http://127.0.0.1:8000/admin/simulate/shipping?down=true" -H "Authorization: Bearer $TOKEN"

$ curl -i http://127.0.0.1:8000/orders/7/shipping    # three times
HTTP/1.1 502 Bad Gateway

{"detail":"The shipping service failed"}

$ curl -i http://127.0.0.1:8000/orders/7/shipping
HTTP/1.1 503 Service Unavailable
retry-after: 30

{"detail":"Service Unavailable"}
```

The status page reads the shared state from the backend, and the breaker from this process:

```console
$ curl http://127.0.0.1:8000/admin/status -H "Authorization: Bearer $TOKEN"
{"maintenance":{"enabled":false,"message":null,"retry_after":null,"since":null},"flags":{"new-checkout":{...},"beta-reports":{...}},"process":{"pid":831700,"shipping_breaker":"open"}}
```

The breaker stays open for 30 s, even when the service is back. Then a single request tries it,
and its success closes the breaker:

```console
$ curl -X PUT "http://127.0.0.1:8000/admin/simulate/shipping?down=false" -H "Authorization: Bearer $TOKEN"

$ curl -i http://127.0.0.1:8000/orders/7/shipping
HTTP/1.1 503 Service Unavailable
retry-after: 30

{"detail":"Service Unavailable"}

$ sleep 30; curl http://127.0.0.1:8000/orders/7/shipping
{"order_id":7,"carrier":"Alhambra Express","days":2}
```

Or, once you know the service is back, close the breaker at once. Only in the process that gets
the request: each process has its own.

```console
$ curl -i -X POST http://127.0.0.1:8000/admin/breakers/shipping/close -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content

$ curl http://127.0.0.1:8000/orders/7/shipping
{"order_id":7,"carrier":"Alhambra Express","days":2}
```

## What to look at

- **The admin token is compared in constant time, as bytes:** `secrets.compare_digest` takes the
  same time however much of a guess is right. It compares bytes because on strings it raises when
  a client sends non-ASCII characters: a 500 instead of a 401.
- **Shared state, local copies:** the switch and the flags are in the backend, signed with
  `CACHE_SIGNING_KEY`, so a state planted there by someone else is ignored. Each process reads
  them again at most every `refresh` seconds, so a change takes up to 1 s (maintenance) or 5 s
  (flags) to reach the other processes; the process that made it, and `/admin/status`, see it at
  once. The breaker is created in the lifespan, one per process: it opens after that process's
  failures, with no round trip on every call.
- **`/health` and `/admin` are always served:** the orchestrator doesn't restart processes that
  are only in maintenance, and the API that closed the app can reopen it.
- **Flag names are an enum:** no flag the app never reads can be created. A flag with an allow
  list and no percentage is only for the list, so a beta never goes to everyone by mistake.
- **Subjects come from authentication:** a route hidden by `flags.require()` takes its subject
  from a function of the request. Here the bearer token is the user id; in a real app take it
  from a session or a token you verify, or anyone could put themselves on an allow list.
- **Only what says the service is down opens the breaker:** `failures=(ConnectionError,)`, so a
  bug of ours doesn't, and a call that hangs is cancelled after 5 s and counts too. A failure is a
  502; a refusal while the breaker is open, a 503 with `Retry-After`.

## The code

<!-- fmt: off -->
```python title="examples/operations/main.py"
--8<-- "examples/operations/main.py"
```
<!-- fmt: on -->
