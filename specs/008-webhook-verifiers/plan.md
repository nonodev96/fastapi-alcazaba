# Plan: webhook verifiers

`webhooks/signing.py`: `_Scheme` with `read(headers)` and `check(delivery, body)`; four schemes;
`WebhookSigner` signs v1 and v1a. Tests: `tests/unit/test_webhooks.py` (+ vectors), fuzz target
over every scheme. Docs: webhooks guide, CHANGELOG. Constitution: secure by default (one 401,
constant time), tests, simplicity (one dependency) — pass.
