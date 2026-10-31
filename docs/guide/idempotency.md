# Idempotency keys

In an app: [the example](../examples/idempotency.md). Every class and function: [the API reference](../reference/idempotency.md).

A client that retries a POST after a timeout or a dropped connection gets the first response
again, instead of creating a second order or paying twice
([draft-ietf-httpapi-idempotency-key-header](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)):

```python
from fastapi_alcazaba.idempotency import IdempotencyMiddleware, require_idempotency_key

app.add_middleware(IdempotencyMiddleware)  # the app's cache (Cache.install) or FastAPICache's


@app.post("/payments", status_code=201, dependencies=[Depends(require_idempotency_key)])
async def pay(payment: Payment) -> Receipt: ...
```

The client sends a fresh key with each new request, and the same one when it retries it:
`Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"`.

- **Retries get the first response** (status, headers and body, but no `Set-Cookie`) with
  `Idempotent-Replayed: true`; the endpoint runs once.
- **One at a time:** a retry that arrives while the first request still runs gets a 409 with
  `Retry-After: 1`. A lock in the backend, renewed while the request runs, sees to it across
  processes.
- **The same key for another request** (method, path, query or body) gets a 422. The body is
  compared byte for byte: a retry sends the same bytes, not the same JSON written another way
  (keys in another order, other spacing).
- **Per client:** keys are looked up among the requests with the same `Authorization` header
  (hashed), so one user never gets another's response. With cookie sessions, and with access
  tokens that get refreshed (a retry sent with a fresh token would run again), pass
  `identifier=` returning the user's id. Requests without credentials share one space of keys,
  safe as long as clients make their keys random (UUIDs).
- **Replays come before authentication:** the route's dependencies don't run for a retry, so a
  copy of the request (token, key and body, from a log) gets the response again until it
  expires, even once the token is revoked. An async `identifier=` that checks the credentials,
  returning the user's id or raising a 401, closes that; a shorter `expire` narrows it.
- **What's kept, and for how long:** responses below 500 for `expire` seconds (24 hours), except
  those worth retrying (408, 409, 425, 429) and those about the request itself, given before the
  endpoint did anything (401, 403, 404, 405, 413, 415, 422 and the like): a retry gets them
  afresh, and nobody can fill the backend with them (FastAPI's 422 echoes the request body).
  Request bodies are read whole to fingerprint them, up to `max_body_size` (a 413 beyond); a
  client that goes away before sending all of it doesn't reach the endpoint. Responses above
  `max_response_size` aren't kept, so a retry runs again. The stored responses go through the
  cache's signing or encryption: use an `encryption_key` when they carry personal data.
- **Failures:** if the backend fails the request gets a 503: running a payment twice is worse
  than refusing it. `fail_open=True` runs it without idempotency instead. Errors of the endpoint
  itself, `BackendUnavailable` included, are left to the app.
- Requests without the header go through untouched. It applies to POST and PATCH (`methods=`),
  and needs a backend with locks.
