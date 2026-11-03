# Feature Specification: Sending webhooks reliably

**Feature Branch**: `feature/webhook-sender` · **Created**: 2026-11-03 · **Status**: Draft

**Input**: "Apps should be able to send webhooks reliably and safely to URLs their users register."

## Clarifications

### Session 2026-11-03

No question was put to the maintainer, who asked for it to be built and merged; the choices are
in research.md. No backward compatibility is kept.

- Deliveries are tasks of a `FastAPITaskQueue` (retries, backoff, timeouts, dead letters, unique
  keys): the sender registers its task on the app's queue (R1).
- Endpoints are the app's (its database): the sender asks `endpoints(endpoint_id)` for the URL and
  the secret at each attempt, so no secret travels in the queue (R2).
- An endpoint is disabled after `disable_after` (20) consecutive failed attempts, or at once when
  it answers 410 Gone; `enable()` turns it back on (R4).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Deliver, at least once (P1)

**Acceptance**: `await sender.send(endpoint_id, payload, message_id=...)` enqueues a delivery; a
worker POSTs the JSON signed the Standard Webhooks way, with the same `webhook-id` at every
attempt; a non-2xx answer or a transport error is retried with backoff until `retries`; sending
the same message to the same endpoint twice while it's pending adds nothing.

### User Story 2 - Safe destinations (P1)

**Acceptance**: URLs that resolve to private, loopback or metadata addresses are refused
(SSRF guard); redirects aren't followed; each attempt has a deadline; the response body is read up
to a small limit and never stored.

### User Story 3 - Endpoints that keep failing (P2)

**Acceptance**: after `disable_after` consecutive failures (or a 410) the endpoint is disabled:
its pending deliveries are skipped; `health(endpoint_id)` says so; `enable()` resets it; a success
resets the count.

### User Story 4 - Delivery records (P2)

**Acceptance**: `delivery(endpoint_id, message_id)` gives, as of its last attempt, the state
(retrying, succeeded, failed, skipped), attempts, the last status code and error type, when it changed; never the body
nor the secret.

### User Story 5 - From the outbox (P2)

**Acceptance**: the guide's recipe writes the webhook in the outbox, in the change's transaction,
and the relay's `deliver` calls `send` with the message's id: nothing is sent for a rollback, and
a message relayed twice is delivered once.

## Requirements

- **FR-001**: Signed (Standard Webhooks), SSRF-guarded, retried, at least once, stable id.
- **FR-002**: Per-endpoint consecutive failures, disabling, re-enabling.
- **FR-003**: Delivery records without bodies or secrets.
- **FR-004**: A `webhooks` extra with httpx2.

## Success Criteria

- **SC-001**: Unit tests with a fake receiver; a live test through the task queue on Redis.
- **SC-002**: A guide section with the outbox recipe.

## Out of Scope

- A UI or an API for end users to manage their endpoints.
