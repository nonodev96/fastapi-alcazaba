# Webhooks and outbound calls

Webhooks verified (Standard Webhooks, GitHub, Stripe, Slack), signed and sent the Standard Webhooks
way, and calls to other services with a deadline, a circuit breaker and SSRF protection.

In an app: [the example](../examples/webhooks.md). Every class and function: [the API reference](../reference/webhooks.md).

## Signed webhooks

`FastAPIWebhooks` accepts only webhooks signed with your secret, and each delivery once.
`WebhookSigner` signs the ones you send. Both follow
[Standard Webhooks](https://www.standardwebhooks.com), the format of Svix, Supabase, Clerk,
Resend, OpenAI and others: `webhook-id`, `webhook-timestamp` and
`webhook-signature: v1,<base64 HMAC-SHA256 of "id.timestamp.body">`, with `whsec_...` secrets.

```python
from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookMessage, WebhookSigner

webhooks = FastAPIWebhooks(settings.webhook_secret)  # "whsec_..."


@app.post("/webhooks/payments")
async def payment_event(message: Annotated[WebhookMessage, Depends(webhooks)]) -> None:
    event = message.json()
    ...  # idempotent on message.id: retries of a message keep its id


# Sending them: sign the exact bytes you send.
signer = WebhookSigner(settings.webhook_secret)  # WebhookSigner.generate_secret() makes one
body = json.dumps(event).encode()
await client.post(url, content=body, headers=signer.sign(body))
```

- **Raw body, still readable:** the signature is checked over the body exactly as it arrived,
  read up to `max_body_size` bytes (1 MiB; a 413 beyond, and a `Content-Length` over it is
  refused before reading). It stays in the request, for `message.json()` or `await
  request.json()`. Validate it with your model there (`Event.model_validate_json(message.body)`)
  rather than with a pydantic body parameter: FastAPI reads and validates those before any
  dependency runs, so the body would be read without the limit, and an unsigned malformed one
  would get a 422 before its signature is checked. Not a form parameter either, which consumes
  the body first.
- **Freshness:** a timestamp more than `tolerance` seconds (300) away from the server's clock,
  in the past or the future, is refused.
- **Constant time, one answer:** every signature in the header is compared in constant time
  against each secret. A wrong signature, a stale timestamp and a replay all get the same 401
  (`"Invalid webhook"`); only missing or malformed headers get a 400.
- **Replays:** each accepted delivery (its id and timestamp, as a SHA-256 digest under
  `Cache.state_key`) is counted in the cache's backend until its timestamp can't be accepted
  anymore, so a captured request sent again is refused by every process. Only accepted
  deliveries are counted: a forged copy can't burn the id of the real one. A retry of the sender
  has a new timestamp and signature and goes through, as it must after a failed attempt: make the
  endpoint idempotent on `message.id` (a unique constraint) if a message must not be processed
  twice. The backend must support counters, as every built-in backend does.
- **Backend down:** a 503 with `Retry-After` (fail closed): senders retry later, while a replay
  would run its effects again. `fail_open=True` accepts correctly signed webhooks without the
  replay check instead.
- **Rotation:** `FastAPIWebhooks([new, old])` accepts a signature by either secret;
  `WebhookSigner([new, old])` signs every message with both, so receivers with either secret
  accept it.
- **Without a dependency:** `webhooks.verify(headers, body)` checks the signature and the
  timestamp (not replays), for queues and scripts.

### Ed25519 signatures (v1a)

Standard Webhooks also signs with Ed25519 (`webhook-signature: v1a,<base64>`): receivers verify
with the sender's public key alone, so no shared secret leaks from a receiver. With the `crypto`
extra:

```python
private, public = WebhookSigner.generate_key_pair()  # "whsk_...", "whpk_..."
signer = WebhookSigner(private)  # or [hmac_secret, private]: v1 and v1a in one header
webhooks = FastAPIWebhooks(public)  # the receiver's: verifies, can't sign
```

A `whsk_` key is the base64 of the 32-byte seed (or libsodium's 64 bytes, seed and public key).

### GitHub, Stripe and Slack

`scheme=` verifies the webhooks of these senders with the same checks: the raw body and its size
limit, constant time, the tolerance on timestamps, replay marks, rotation with a list of secrets,
and one 401 for every failure.

```python
github = FastAPIWebhooks(settings.github_webhook_secret, scheme="github")
stripe = FastAPIWebhooks(settings.stripe_webhook_secret, scheme="stripe")  # "whsec_..." as given
slack = FastAPIWebhooks(settings.slack_signing_secret, scheme="slack")
```

| Scheme | What's checked | `message.id` | `message.timestamp` |
| --- | --- | --- | --- |
| `github` | `X-Hub-Signature-256: sha256=<HMAC of the body>` | a digest of what's signed | None |
| `stripe` | `Stripe-Signature: t=<time>,v1=<HMAC of "time.body">` (any of its `v1`) | a digest of what's signed | `t` |
| `slack` | `X-Slack-Signature: v0=<HMAC of "v0:time:body">`, `X-Slack-Request-Timestamp` | a digest of what's signed | the header |

- **GitHub signs no time, nor `X-GitHub-Delivery`:** a copy could change that header, so the
  message is named by its body (a redelivery sends the same one). A delivery is refused again
  only within the replay window (twice `tolerance`, plus a minute); a capture sent later goes
  through. Keep the endpoint idempotent on `message.id`.
- **Stripe's secret** is used as the string it gives you, `whsec_` included (not decoded as
  Standard Webhooks' are); its event id is in the body (`message.json()["id"]`).
- **Slack** sends slash commands and interactions as forms: take the body with
  `await request.form()` after the dependency (it keeps the body), not as form parameters.
- Their secrets are at least 16 characters.

## Sending webhooks

`WebhookSender` delivers webhooks to the URLs your users register, through the
[task queue](background.md#task-queue): signed the Standard Webhooks way, through the SSRF guard,
retried with backoff until the receiver takes them, at least once. With the `webhooks` extra.

```python
from fastapi_alcazaba.background.outbox import OutboxMessage, OutboxRelay
from fastapi_alcazaba.webhooks import WebhookEndpoint, WebhookSender


async def endpoint(endpoint_id: str) -> WebhookEndpoint | None:
    row = await endpoints_table.get(endpoint_id)  # yours: the URL and the secret the user got
    return WebhookEndpoint(row.url, row.secret) if row is not None else None


sender = WebhookSender(queue, endpoint)  # before the queue starts: it registers its task


# The webhook is written with the change, in its transaction, and sent once it's committed.
@app.post("/orders/{order_id}/pay")
async def pay(order_id: int) -> None:
    async with async_session() as session, session.begin():
        ...  # the change
        for endpoint_id in await subscribed(session, "order.paid"):
            event = {"type": "order.paid", "data": {"order_id": order_id}}
            await outbox.add(session, "webhook", {"endpoint": endpoint_id, "event": event})


async def deliver(message: OutboxMessage) -> None:
    payload = message.payload
    await sender.send(payload["endpoint"], payload["event"], message_id=message.id)


relay = OutboxRelay(outbox, deliver)
```

- **At least once, one id:** each (endpoint, message) is one task of the queue, enqueued once
  while it's pending (`unique_key`): a message the outbox relays twice is delivered once, and every
  attempt carries the same `webhook-id`, for the receiver to deduplicate on. The body is
  serialised once, at `send()`; each attempt is signed afresh.
- **Retries:** a 2xx is delivered; any other status or a transport error is tried again, `retries`
  times (8) with exponential backoff and jitter from `backoff` (5 s) up to `max_backoff` (6 h).
  Then the task goes to the queue's dead letters, where `requeue_dead_letters()` or
  `fastapi-alcazaba tasks ... requeue` sends it again.
- **Safe destinations:** its own client refuses URLs that resolve to private, loopback or
  metadata addresses (`SSRFGuardTransport`; `allow=` for an internal receiver), doesn't follow
  redirects, gives each attempt `timeout` seconds (10) and reads at most 64 KiB of the answer,
  only for its status. `client=` gives yours instead, and the guard is then yours to add.
- **Endpoints that keep failing:** after `disable_after` (20) failed attempts in a row, or at
  once when the receiver answers 410 Gone, the endpoint is disabled: its deliveries are skipped,
  not retried. Each endpoint's failures are its own: its client has no circuit breaker per host,
  so one user's endpoint that fails never blocks nor counts against another user's on the same
  host. `health(endpoint_id)` says how many failures in a row and whether it's disabled;
  `enable()` turns it back on (send the skipped messages again), `disable()` turns it off.
- **Records:** `delivery(endpoint_id, message_id)` gives its state as of its last attempt
  (retrying, succeeded, failed, skipped), the attempts, the last status code and error type, and
  when; never the body nor the secret. Kept `record_ttl` (7 days).
- **Secrets stay with you:** the queue carries the endpoint's id, the message's id and the body;
  `endpoints()` is asked for the URL and the secret at each attempt, so a rotated secret or a
  changed URL is used at once, and an endpoint deleted skips its deliveries.
- One sender per queue; `await sender.aclose()` at shutdown closes its client.

## Outbound calls

`ResilientTransport` is an httpx transport for the calls your app makes to other services: a
deadline for each call and a circuit breaker for each origin. It needs httpx2 (or httpx):

```python
import httpx2 as httpx

from fastapi_alcazaba.outbound import CircuitOpenError, ResilientTransport


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    transport = ResilientTransport(timeout=5, failure_threshold=5, reset_timeout=30)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.example.com") as client:
        app.state.partner_api = client
        yield


@app.get("/quote")
async def quote(request: Request) -> Quote:
    try:
        response = await request.app.state.partner_api.get("/quote")
    except CircuitOpenError:  # the service keeps failing: don't wait for it
        raise HTTPException(503, "Quotes are unavailable, try again later")
    return Quote.model_validate(response.json())
```

- **Deadline:** `timeout` bounds the whole call, from sending the request to the last byte of the
  response. httpx's own timeouts are per phase, and its read timeout restarts with every byte, so
  a server that trickles a byte every few seconds would keep the request open forever. Past the
  deadline, `httpx.TimeoutException`. It applies to each request the transport sends: with
  `follow_redirects=True`, every hop gets a deadline of its own.
- **Response size:** responses are read whole before they're returned, so don't stream large
  downloads through it. `max_response_size` caps the bytes read (as received, before
  decompression); past it, `ResponseTooLargeError`, an `httpx.RequestError`.
- **Circuit breaker:** each origin (scheme, host and port) has its own. After
  `failure_threshold` consecutive failures, calls to that origin fail at once with
  `CircuitOpenError` for `reset_timeout` seconds, without reaching the server; then a single
  trial call decides whether it's back. `CircuitOpenError` is an `httpx.TransportError`, so code
  that already handles httpx errors handles it too. `transport.state(url)` says `"closed"`,
  `"open"` or `"half-open"`, handy in a health check. `failure_threshold=None` turns it off.
- **What counts as a failure:** the origin's transport errors (connection and protocol errors,
  timeouts, yours and httpx's) and responses whose status is in `failure_statuses` (500, 502, 503
  and 504 by default), which are still returned to you. Any other response, 4xx included, is a
  success. Your own errors are neither: an illegal header value (`httpx.LocalProtocolError`), a
  URL scheme httpx can't send (`httpx.UnsupportedProtocol`), an exception from your request body,
  `ResponseTooLargeError`. Otherwise anyone who can put a string in an outbound header could
  switch the integration off for everybody.
- **Your own transport settings:** `ResilientTransport(httpx.AsyncHTTPTransport(verify=...,
  limits=..., proxy=...))`.

### URLs your users give: SSRF

Calling a URL that a user typed (a webhook endpoint, a link preview, an avatar by URL) lets them
make your server call its own network: `http://169.254.169.254/` (the cloud's metadata service
and its credentials), `http://localhost:6379/`, an admin panel on `10.0.0.0/8`.
`SSRFGuardTransport` only calls the public internet:

```python
from fastapi_alcazaba.outbound import (
    BlockedDestinationError,
    ResilientTransport,
    SSRFGuardTransport,
)

client = httpx.AsyncClient(
    transport=ResilientTransport(SSRFGuardTransport(ports=(443,)), timeout=10),
    follow_redirects=True,
)

try:
    await client.post(subscription.url, content=body, headers=signer.sign(body))
except BlockedDestinationError:
    ...  # tell the user the URL isn't allowed
```

- **What's refused:** a host with any address that isn't public: private, loopback, link-local
  (169.254.0.0/16 and its metadata services, `fe80::/10`), carrier-grade NAT (100.64.0.0/10),
  multicast, reserved, documentation and benchmarking ranges, unique-local IPv6 (`fc00::/7`, AWS's
  `fd00:ec2::254`), and the IPv6 forms of those IPv4 addresses (`::ffff:127.0.0.1`, NAT64
  `64:ff9b::/96`, 6to4, Teredo). Literal addresses in URLs are checked without DNS; anything else
  goes to DNS, so numeric spellings (`http://2130706433/`, `http://0x7f.1/`) and `localhost`
  are refused for what they resolve to. Also refused: schemes other than http and https and, with
  `ports`, other ports. `BlockedDestinationError` says which host, never the address it resolved
  to (that's logged instead).
- **Pinned against DNS rebinding:** the transport resolves the host itself and connects to the
  address it checked: the URL is rewritten to it, the `Host` header keeps the name, and TLS gets
  the name for SNI and the certificate check. A DNS server that answers a public address to the
  check and a private one to the connection gets nowhere. If the first address can't be reached,
  one of the other family (IPv4/IPv6) is tried.
- **Redirects:** with `follow_redirects=True`, every hop goes through the transport and is checked
  like the first one.
- **Checking a URL when it's given:** `await guard.check(url)` checks it as a request would be
  (scheme, port, every address its host resolves to) without sending anything, so a webhook's
  endpoint can be refused when a user registers it. Each request is checked again all the same:
  DNS answers change.
- **Allowed internal destinations:** `allow=["billing.internal", "*.svc.cluster.local",
  "10.1.2.0/24"]` lets those names (exact, or subdomains) and networks through although they
  aren't public; they're still pinned.
- **With `ResilientTransport`:** wrap the guard in it (not the other way round): the deadline
  then covers the DNS lookup and each breaker is per host name. `BlockedDestinationError` is an
  `httpx.RequestError` but not a `TransportError`, so it doesn't count against the origin's
  breaker or trigger retry policies.
- **Connections:** each host name gets an `httpx.AsyncHTTPTransport` of its own (sharing one SSL
  context), at most `max_hosts` (100) kept open: pinned connections are pooled by address, and two
  names on one address (a CDN) must not share a connection whose certificate was checked for the
  other. Pass a factory for your own settings, `SSRFGuardTransport(lambda:
  httpx.AsyncHTTPTransport(http2=True))`, or a transport instance (a mock in tests), which then
  carries every request.
- **Proxies:** a proxy connects on its own, so pinning can't work through it:
  `httpx.AsyncHTTPTransport(proxy=...)` as the inner transport is refused. Don't pass `proxy=` or
  `mounts=` to the client either: those requests would bypass the transport. (The client ignores
  the `HTTP_PROXY` variables when it's given a transport.) An egress proxy that filters
  destinations itself, such as Smokescreen, is the alternative.
