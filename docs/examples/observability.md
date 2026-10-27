# Errors and observability

An orders API that is easy to operate. Its errors are RFC 9457 problem details, from
[`FastAPIProblemDetails`](../index.md#problem-details): a validation error says where and what
without echoing what was sent, running out of stock is a [`Problem`](../index.md#problem-details)
with an `available` member, and a bug is a bare 500 that tells the client nothing about it.
[`RequestIDMiddleware`](../index.md#request-ids) gives every request an ID, which `RequestIDLogFilter`
puts in every log line, uvicorn's included, and which comes back in a header and in every problem.

[`RequestTimeoutMiddleware`](../index.md#request-timeouts) gives every request a deadline: the slow
sales report gets a 504 and its database connection back, while the CSV export streams for as long
as it takes. [`health_router`](../index.md#health-checks) adds the probes of the load balancer,
with the database as a critical check and the cache as a non-critical one. OpenTelemetry needs no
code: under its zero-code instrumentation, Alcazaba's metrics and spans go out with the app's
([Metrics and traces](../index.md#metrics-and-traces)).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
python examples/observability/main.py
```

That starts uvicorn with the example's `LOGGING` configuration, so that every log line carries the
request's ID. `uvicorn examples.observability.main:app` serves the same app with uvicorn's
default logging, without the IDs; to use the configuration with the `uvicorn` command, save it as
JSON and pass `--log-config`.

## Try it

An order placed. The response has an `X-Request-ID`, and so does every line logged for it:

```console
$ curl -i -X POST http://127.0.0.1:8000/orders -H "Content-Type: application/json" \
    -d '{"product_id": 1, "quantity": 2}'
HTTP/1.1 201 Created
content-type: application/json
x-request-id: efdd515c85e68aefadfe379f34142eef

{"product_id":1,"quantity":2,"id":31}
```

```console
2026-10-26 12:34:23,679 INFO [efdd515c85e68aefadfe379f34142eef] orders: Order 31 placed
2026-10-26 12:34:23,680 INFO [efdd515c85e68aefadfe379f34142eef] "POST /orders HTTP/1.1" 201 Created
```

A validation error says which field is wrong and why, but not what was sent: FastAPI's own answer
would echo the input, passwords included.

```console
$ curl -i -X POST http://127.0.0.1:8000/orders -H "Content-Type: application/json" \
    -d '{"product_id": 1, "quantity": "lots"}'
HTTP/1.1 422 Unprocessable Content
x-request-id: f25fd411982ef2db845915fd6c6591b6
content-type: application/problem+json

{"type":"about:blank","title":"Unprocessable Content","status":422,"instance":"/orders","errors":[{"loc":["body","quantity"],"msg":"Input should be a valid integer, unable to parse string as an integer","type":"int_parsing"}],"request_id":"f25fd411982ef2db845915fd6c6591b6"}
```

Running out of stock is a problem of the API's own, with its `type` and an `available` member that
a client can act on:

```console
$ curl -i -X POST http://127.0.0.1:8000/orders -H "Content-Type: application/json" \
    -d '{"product_id": 2, "quantity": 5}'
HTTP/1.1 409 Conflict
x-request-id: 3905c40b8fa0c37707adf6b910773f47
content-type: application/problem+json

{"type":"https://example.com/problems/out-of-stock","title":"Out of stock","status":409,"detail":"Only 3 left","instance":"/orders","available":3,"request_id":"3905c40b8fa0c37707adf6b910773f47"}
```

A bug: the invoice's PDF renderer refuses the connection, and nothing handles it. The client gets a
bare 500 with nothing about the renderer, and the request ID to quote. Here the client sent the ID
itself, as a proxy would, and it was kept:

```console
$ curl -i http://127.0.0.1:8000/orders/3/invoice -H "X-Request-ID: support-ticket-4512"
HTTP/1.1 500 Internal Server Error
x-request-id: support-ticket-4512
content-type: application/problem+json

{"type":"about:blank","title":"Internal Server Error","status":500,"instance":"/orders/3/invoice","request_id":"support-ticket-4512"}
```

The server logs the traceback under that ID:

```console
2026-10-26 12:34:23,692 INFO [support-ticket-4512] "GET /orders/3/invoice HTTP/1.1" 500 Internal Server Error
2026-10-26 12:34:23,692 ERROR [support-ticket-4512] uvicorn.error: Exception in ASGI application
Traceback (most recent call last):
  ...
  File ".../examples/observability/main.py", line 140, in invoice
    raise ConnectionError(f"pdf-renderer.internal:9000 refused the invoice of order {order_id}")
ConnectionError: pdf-renderer.internal:9000 refused the invoice of order 3
```

The sales report takes 30 s. At the 10 s deadline the query is cancelled, its connection goes back
to the pool and the client gets a 504. The middleware answers it itself, and with
`FastAPIProblemDetails` installed its answer is a problem too:

```console
$ curl -i http://127.0.0.1:8000/reports/sales
HTTP/1.1 504 Gateway Timeout
x-request-id: 7ec602139e1478ad0c023637123c599e
content-type: application/problem+json

{"type":"about:blank","title":"Gateway Timeout","status":504,"detail":"Request timed out","instance":"/reports/sales","request_id":"7ec602139e1478ad0c023637123c599e"}
```

```console
2026-10-26 15:22:34,756 WARNING [7ec602139e1478ad0c023637123c599e] fastapi_alcazaba.observability.timeouts: GET /reports/sales timed out after 10.0s: answered 504
```

The export has no deadline: its 30 rows stream for 15 s, and it isn't cut short.

```console
$ curl -s -o /dev/null -w '%{http_code} %{content_type} %{size_download} bytes in %{time_total}s\n' \
    http://127.0.0.1:8000/orders/export
200 text/csv; charset=utf-8 231 bytes in 15.539356s
```

The probes. Readiness runs the checks and says how each went; liveness only says that the process
answers:

```console
$ curl -i http://127.0.0.1:8000/health/ready
HTTP/1.1 200 OK
cache-control: no-store
content-type: application/json
x-request-id: 577fa3fd67c00a6bd4b80cbf29256384

{"status":"ok","checks":{"database":{"status":"ok","duration_ms":0.014},"cache":{"status":"ok","duration_ms":0.012}}}

$ curl http://127.0.0.1:8000/health/live
{"status":"ok"}
```

## What to look at

- **The order of the middlewares:** the last one added is the outermost. `RequestIDMiddleware`
  goes around the deadline, so that the 504 has an ID too. A 500 is answered outside every
  middleware, by Starlette, and `FastAPIProblemDetails` adds the ID to it itself.
- **The filter on the handlers:** a logger's filters don't see the records of its children, so
  `RequestIDLogFilter` goes on both handlers of the logging configuration, and uvicorn's access
  and error lines get the ID along with the app's.
- **Nothing to learn from an error:** validation errors never carry the input, the 500 says
  nothing about the renderer, and the database's `db.internal:5432: connection refused` would be
  logged by the readiness check, never returned to a public probe.
- **A deadline gives resources back:** the connection is a `yield` dependency, so it goes back to
  the pool when the deadline cancels the request. The export is exempt
  (`paths={"/orders/export": None}`): once a response has started it can't become a 504, it would
  only be cut short.
- **Critical or not:** a failing database makes `/health/ready` a 503 and takes the process out of
  the load balancer; a failing cache only makes it `degraded`, since the cache fails open and
  taking every process out would turn its outage into a full one.
- **OpenTelemetry without code:** run the app under `opentelemetry-instrument`. The FastAPI
  instrumentation traces each request, and Alcazaba's metrics (`fastapi_alcazaba.problems`,
  `.request_timeouts`, `.request_ids`, `.health.checks`…) go out through the same SDK.

## The code

<!-- fmt: off -->
```python title="examples/observability/main.py"
--8<-- "examples/observability/main.py"
```
<!-- fmt: on -->
