# Research: webhook verifiers

Read `webhooks/signing.py`, its tests and fuzz target; GitHub, Stripe, Slack and Standard
Webhooks documentation, on 2026-11-03. GitHub's and Slack's examples were checked by hand.

## R1. One dependency, a scheme each

- **Decision**: `FastAPIWebhooks(secret, scheme="standard")`. A scheme reads its headers into a
  delivery (id, timestamp as sent or None, signatures) and checks a signature against the body;
  the dependency does the rest (size, tolerance, replay marks, metrics), the same for all.

| Scheme | Headers | Signed | Secret |
| --- | --- | --- | --- |
| standard | `webhook-id`, `webhook-timestamp`, `webhook-signature` (`v1,<b64>` / `v1a,<b64>`) | `id.ts.body` | `whsec_` (HMAC), `whpk_` (Ed25519 public) |
| github | `X-GitHub-Delivery`, `X-Hub-Signature-256: sha256=<hex>` | `body` | the string |
| stripe | `Stripe-Signature: t=<ts>,v1=<hex>[,v1=…]` | `ts.body` | the string `whsec_…` itself |
| slack | `X-Slack-Request-Timestamp`, `X-Slack-Signature: v0=<hex>` | `v0:ts:body` | the string |

## R2. Ed25519 (v1a)

- **Decision**: `whpk_<base64 of 32 bytes>` verifies, `whsk_<base64 of 32 or 64 bytes>` (a seed,
  or libsodium's seed + public key) signs; `cryptography` (already in the `crypto` and JWT extras)
  imported when such a key is given.

## R3. Ids and replays

- **Decision**: the replay mark is (id, timestamp): standard's id; GitHub's delivery id (no
  timestamp: marked for the same window); Stripe and Slack, which send no id, a SHA-256 of what
  they signed, which is also `WebhookMessage.id`. `WebhookMessage.timestamp` is None for GitHub.
- **Rationale**: a sender's retry is signed afresh (new timestamp) and gets through; a capture
  sent again within the window doesn't. GitHub can't be protected beyond the window.

## R4. Secrets

- **Decision**: GitHub, Stripe and Slack secrets are strings of at least 16 characters.
