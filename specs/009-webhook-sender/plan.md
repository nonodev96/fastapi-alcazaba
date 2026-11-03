# Plan: webhook sender

`fastapi_alcazaba/webhooks/sender.py` (new): `WebhookEndpoint`, `WebhookDelivery`,
`EndpointHealth`, `WebhookSender`. Built on `WebhookSigner`, `ResilientTransport`,
`SSRFGuardTransport`, `FastAPITaskQueue`, the cache's backend for records. `webhooks` extra.
Tests: `tests/unit/test_webhook_sender.py` (fake receiver: httpx MockTransport), a live test on
Redis through the queue. Docs: webhooks guide (recipe with the outbox), observability, CHANGELOG.
Constitution: SSRF guard by default, no secrets stored, tests, simplicity (no endpoint store of
ours) — pass.
