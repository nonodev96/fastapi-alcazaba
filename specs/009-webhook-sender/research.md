# Research: sending webhooks

Read `webhooks/signing.py`, `outbound/transports.py`, `background/tasks.py` and
`background/outbox.py`, and Standard Webhooks' recommendations for senders, on 2026-11-03.

## R1. Deliveries are tasks

- **Decision**: `WebhookSender(queue, endpoints, ...)` registers `fastapi_alcazaba.webhooks.deliver`
  on the queue with `retries` (8), `backoff` (5 s), `max_backoff` (6 h) and a `timeout`.
  `send()` enqueues it with `unique_key = endpoint id + message id`: the outbox's redeliveries and
  the app's retries add nothing while a delivery is pending.
- **Rationale**: the queue has retries with jitter, dead letters, timeouts, status, a CLI.

## R2. Endpoints from the app

- **Decision**: `endpoints(endpoint_id) -> WebhookEndpoint | None` (sync or async): `url`, `secret`
  (a list rotates). Asked at each attempt: a URL changed or an endpoint deleted is seen at once,
  and the secret never goes to the queue. None: the delivery is skipped.

## R3. Sending

- **Decision**: httpx2 with `ResilientTransport(SSRFGuardTransport(allow=...), timeout=10,
  max_response_size=64 KiB)`, no redirects, `Content-Type: application/json`, the body serialised
  once at `send()` (the same bytes at every attempt), signed afresh at each attempt with the
  message id. 2xx: delivered. Anything else, or a transport error: a failure, retried.
  `client=` replaces the client (and the guard with it).

## R4. Failing endpoints

- **Decision**: a counter of consecutive failed attempts per endpoint (`incr`), reset by a success;
  at `disable_after` (20), or at a 410, a "disabled" mark (no expiry). A disabled endpoint's
  deliveries are skipped, not retried. `enable()` removes both; `health()` reads both.

## R5. Records

- **Decision**: one record per (endpoint, message), JSON under a state key, kept `record_ttl`
  (7 days): state, attempts, status code, error type, updated, written by the attempt only (the
  queue runs one delivery of a message to an endpoint at a time). The attempts are the queue's,
  read with `current_task()` (new): a counter of our own, written by `send()` too, lost attempts
  when two processes sent one message (seen in the live test). Bodies and secrets are never
  stored.

## R6. Metrics

- **Decision**: counter `webhooks.deliveries` by outcome: delivered, retried, failed, skipped,
  disabled. No endpoint attribute: ids are the users', unbounded.
