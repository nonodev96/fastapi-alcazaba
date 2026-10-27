# Idempotency keys

A payments API where a client that retries after a timeout or a dropped connection gets the first
response again, instead of charging the card twice.
[`IdempotencyMiddleware`](../guide/idempotency.md) keeps each response under the client's
`Idempotency-Key`, and `require_idempotency_key` makes `POST /payments` refuse requests without
one. The responses are kept in the cache installed on the app, signed with its
[`signing_key`](../guide/security-model.md#security-model).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.idempotency.main:app
```

Stored responses are signed: set `CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)`), the same in
every process, or a retry that reaches another process charges again. The example has two
customers, `alice` and `bob`, whose tokens are `alice-token` and `bob-token`.

## Try it

A payment, with a new key:

```console
$ curl -i -X POST http://127.0.0.1:8000/payments -H "Authorization: Bearer alice-token" \
    -H "Idempotency-Key: 8e03978e-40d5-43e8-bc93-6894a57f9324" \
    -H "Content-Type: application/json" \
    -d '{"amount": 1250, "currency": "EUR", "description": "Order 1042"}'
HTTP/1.1 201 Created

{"amount":1250,"currency":"EUR","description":"Order 1042","id":"ch_182671f5910f3ece","customer":"alice","created_at":"2026-10-26T10:57:18.321689Z"}
```

The same request again, as a client retries it, gets the same response without charging again:

```console
$ curl -i -X POST http://127.0.0.1:8000/payments -H "Authorization: Bearer alice-token" \
    -H "Idempotency-Key: 8e03978e-40d5-43e8-bc93-6894a57f9324" \
    -H "Content-Type: application/json" \
    -d '{"amount": 1250, "currency": "EUR", "description": "Order 1042"}'
HTTP/1.1 201 Created
idempotent-replayed: true

{"amount":1250,"currency":"EUR","description":"Order 1042","id":"ch_182671f5910f3ece","customer":"alice","created_at":"2026-10-26T10:57:18.321689Z"}

$ curl http://127.0.0.1:8000/payments -H "Authorization: Bearer alice-token"
[{"amount":1250,"currency":"EUR","description":"Order 1042","id":"ch_182671f5910f3ece","customer":"alice","created_at":"2026-10-26T10:57:18.321689Z"}]
```

The same key with another body is a bug in the client, not a retry:

```console
$ curl -i -X POST http://127.0.0.1:8000/payments -H "Authorization: Bearer alice-token" \
    -H "Idempotency-Key: 8e03978e-40d5-43e8-bc93-6894a57f9324" \
    -H "Content-Type: application/json" \
    -d '{"amount": 99000, "currency": "EUR", "description": "Order 1042"}'
HTTP/1.1 422 Unprocessable Content

{"detail": "This Idempotency-Key was used with a different request"}
```

Keys are per customer: Bob sending Alice's key gets a payment of his own, not hers:

```console
$ curl -i -X POST http://127.0.0.1:8000/payments -H "Authorization: Bearer bob-token" \
    -H "Idempotency-Key: 8e03978e-40d5-43e8-bc93-6894a57f9324" \
    -H "Content-Type: application/json" \
    -d '{"amount": 1250, "currency": "EUR", "description": "Order 1042"}'
HTTP/1.1 201 Created

{"amount":1250,"currency":"EUR","description":"Order 1042","id":"ch_f9647245e9b65cf9","customer":"bob","created_at":"2026-10-26T10:57:19.339093Z"}
```

A payment without a key is refused:

```console
$ curl -i -X POST http://127.0.0.1:8000/payments -H "Authorization: Bearer alice-token" \
    -H "Content-Type: application/json" \
    -d '{"amount": 1250, "currency": "EUR", "description": "Order 1042"}'
HTTP/1.1 400 Bad Request

{"detail":"This request needs an Idempotency-Key header"}
```

A retry that arrives while the first request is still charging the card (it takes a second) gets
a 409 with `Retry-After: 1` and `{"detail": "A request with this Idempotency-Key is still being
processed"}`, and the card is charged once:

```console
$ for attempt in first retry; do \
    curl -s -o /dev/null -w "$attempt: %{http_code} after %{time_total}s\n" \
      -X POST http://127.0.0.1:8000/payments -H "Authorization: Bearer alice-token" \
      -H "Idempotency-Key: 5f0c1a8e-2b7d-4c39-9e61-0d4f8a3b7c25" \
      -H "Content-Type: application/json" \
      -d '{"amount": 4800, "currency": "EUR", "description": "Order 1043"}' & \
    sleep 0.2; \
  done; wait
retry: 409 after 0.000958s
first: 201 after 1.002062s
```

## What to look at

- **Keys per customer, not per token:** `identifier=customer_id` makes the middleware look keys
  up among the requests of the same customer. The default, the `Authorization` header, suits API
  keys that never change; with access tokens that expire, a retry sent with a refreshed token
  would charge the card again.
- **The key is required where it matters:** the middleware handles every POST and PATCH that
  carries an `Idempotency-Key`, and `require_idempotency_key` answers 400 to a payment without
  one. It runs after the authentication, so a request without a token gets a 401 first.
- **What's kept:** the responses the endpoint gives, below 500. Not those refused before anything
  ran (a 401, a 422 for an invalid body): the client fixes the request and sends it with the same
  key. A retry must otherwise send the same body, byte for byte: it's what the 422 for "another
  request" compares.
- **Server errors run again:** a 500 isn't kept, so a retry runs the payment again. That's why the
  key also goes to the provider (`f"{customer}:{key}"`): if the request fails after the charge,
  the provider's own idempotency keeps the card from being charged twice.
- **Signed responses:** the cache's `signing_key` keeps whoever can write to Redis from planting a
  response for a retry. When the responses carry personal data, `encryption_key` encrypts them
  too.
- **Shared state:** InMemoryBackend keeps the responses and locks in one process. With Redis or
  Valkey a retry finds the first response, and the 409, whichever process it reaches.

## The code

<!-- fmt: off -->
```python title="examples/idempotency/main.py"
--8<-- "examples/idempotency/main.py"
```
<!-- fmt: on -->
