# Features

## Alcazaba features

### Secure by default

What could serve one user's data to another, trust data from outside, or let a client get
around a protection is off unless you turn it on, and the documentation says what it costs.

The response cache is safe by default:

- **Users never get each other's responses.** Requests carrying `Authorization` or `Cookie` get
  their own cache entries and the response is sent as `Cache-Control: private`, also when the
  endpoint authenticates through a router dependency.
- **No Redis script injection.** `FastAPICache.clear(namespace=...)` deletes with `SCAN` +
  `UNLINK`: a namespace from user input never reaches a Lua script, and the server isn't blocked
  by `KEYS`.
- **Signed or encrypted entries.** With a `signing_key`, every entry carries an HMAC bound to its
  key: whoever can write to Redis can't plant data, move one user's entry under another user's
  key, or get a pickle executed. With an `encryption_key` they're also encrypted (AES-256-GCM): a
  dump or a backup of Redis doesn't reveal the cached responses.
- **Bounded memory.** `InMemoryBackend` evicts the least recently used entries instead of
  growing forever with every new query string.
- **Clients can't bypass the cache** with `Cache-Control: no-cache` unless you allow it.
- **Only successful responses are cached**, never a 404 or a 500 replayed as a 200.
- **Cache stampede protection**: concurrent misses for one key compute once, in each process
  and, with `distributed_lock`, across processes.
- **A broken backend doesn't take the app down**: every backend call has a timeout, and a circuit
  breaker stops calling it while it fails.
- **Keys that never repeat are reported**: an argument whose `repr()` changes on every call (a
  database session, a service) makes every request a miss; Alcazaba warns about it.

And more: early refresh of popular entries, Valkey through valkey-py or Valkey GLIDE, cache
hits that return the annotated type (Pydantic models, not dicts), deterministic `ETag`s, the
[`Cache-Status`](https://www.rfc-editor.org/rfc/rfc9211) header (`Alcazaba; hit; ttl=42`,
`Alcazaba; fwd=miss; stored`), the response model in OpenAPI, `exclude_params` and
`stale-while-revalidate`.

The other tools follow the same rules. Nothing that opens a door is stored in clear: API keys,
one-time codes and revoked tokens are kept as digests or HMACs. Sessions keep only a random id in
the cookie. Uploads are checked from their bytes, not from what the client says they are.

### Shared by every process

An app runs in several processes, on several machines. Whatever must hold for all of them lives
in the backend, Redis, Valkey, memcached or DynamoDB, and every process sees it at once: the rate
limits and the locks, the sessions and the revoked tokens, maintenance mode and the feature
flags. Scheduled jobs run once per tick whatever the number of processes, and the task queue
runs each task at least once, retried when it fails.

### Resilient

Every call to the backend has a timeout and goes through a circuit breaker: a backend that hangs
or fails doesn't take the app down with it. What each tool does then is chosen for it and
documented: the cache and the rate limiter let requests through, the token blocklist refuses
them. The [security model](../guide/security-model.md) has the details.

### The FastAPI way

The tools are what FastAPI already uses: dependencies (`Depends(FastAPIRateLimiter(...))`,
`Depends(authz)`), decorators (`@cache`) and ASGI middlewares. What they need from the request
shows in the OpenAPI schema, and the docs' **Authorize** button knows the API keys and tokens.
Everything is typed: `@cache` keeps the signature and the return type of what it decorates, and
your editor completes what you write.

### Based on standards

Errors are problem details ([RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)), cache results
are told in `Cache-Status` ([RFC 9211](https://www.rfc-editor.org/rfc/rfc9211)), and rate limits
in the IETF's `RateLimit` headers. Webhooks follow [Standard Webhooks](https://www.standardwebhooks.com),
pages link to each other with `Link` ([RFC 8288](https://www.rfc-editor.org/rfc/rfc8288)), and the
security headers are OWASP's. Authorization uses [Casbin](https://casbin.org) policies, and
metrics and traces go through [OpenTelemetry](https://opentelemetry.io).

### Observable

Alcazaba logs under `fastapi_alcazaba.*` and, with the `otel` extra, records metrics and traces
through OpenTelemetry: cache hits and misses, backend calls, limited requests, refused
authorizations, task runs. Never keys, tokens or identities. Every request can have an ID, in the
logs, the responses and the errors. See [metrics and traces](../guide/observability.md#metrics-and-traces).

### Tested hard

Every change runs the tests on Python 3.11 to 3.14, against real Redis, Valkey, a Valkey cluster,
memcached and DynamoDB Local, with at least 90 % coverage, and the examples run in uvicorn
processes against a real Valkey. Fuzzing (ClusterFuzzLite) runs on whatever reads data from
outside, mutation testing checks that the tests notice when the code changes, and a benchmark
50 % slower than main's fails the pull request.

### Signed releases

Releases are built by GitHub Actions and signed with [Sigstore](https://www.sigstore.dev/), with
an SBOM of what they contain: you can [verify](../verifying.md) that what PyPI serves is what the
repository built. The project's builds take no dependency released less than three days earlier.
