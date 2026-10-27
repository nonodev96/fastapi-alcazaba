# Webhooks in and out

A shop that takes webhooks from its payment provider and sends webhooks of its own to the URLs
its users register. [`FastAPIWebhooks`](../guide/webhooks.md#signed-webhooks) accepts only the provider's
events signed with its secret, and each delivery once. When an order is paid, the shop signs an
`order.paid` event with each user's secret ([`WebhookSigner`](../guide/webhooks.md#signed-webhooks)) and
sends it through [`ResilientTransport`](../guide/webhooks.md#outbound-calls), for a deadline and a
circuit breaker per endpoint, around
[`SSRFGuardTransport`](../guide/webhooks.md#urls-your-users-give-ssrf), so that a user can't point the
shop at its own network.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
export PAYMENTS_WEBHOOK_SECRET=$(python -c "from fastapi_alcazaba.webhooks import WebhookSigner; print(WebhookSigner.generate_secret())")
uvicorn examples.webhooks.main:app
```

In production, the provider gives the secret when you set up the endpoint. Without
`PAYMENTS_WEBHOOK_SECRET` the app makes a throwaway one, which nobody else can sign with.

## Try it

A request without a signature:

```console
$ BODY='{"type": "payment.succeeded", "data": {"order_id": "ord_1", "amount": 1999}}'
$ curl -i http://127.0.0.1:8000/webhooks/payments -d "$BODY"
HTTP/1.1 400 Bad Request

{"detail":"Invalid webhook"}
```

Sign it as the provider would, with the same secret (in the same shell as the `export`), and
keep the headers in a file for curl:

```console
$ sign() { python -c 'import os, sys; from fastapi_alcazaba.webhooks import WebhookSigner; \
    signer = WebhookSigner(os.environ["PAYMENTS_WEBHOOK_SECRET"]); \
    headers = signer.sign(sys.argv[1], message_id="msg_1"); \
    print("\n".join(f"{name}: {value}" for name, value in headers.items()))' "$BODY" > signed.txt; }
$ sign && cat signed.txt
webhook-id: msg_1
webhook-timestamp: 1793012100
webhook-signature: v1,r/kZwT7lk1Z+roS1OQd6pcxNinGLv8NUwlkJ964CYK0=
$ curl -i http://127.0.0.1:8000/webhooks/payments -H @signed.txt -d "$BODY"
HTTP/1.1 204 No Content
```

The same request again, as someone who captured it would send it, and the same signature on
another body:

```console
$ curl -i http://127.0.0.1:8000/webhooks/payments -H @signed.txt -d "$BODY"
HTTP/1.1 401 Unauthorized

{"detail":"Invalid webhook"}
$ curl -i http://127.0.0.1:8000/webhooks/payments -H @signed.txt -d "${BODY/1999/1}"
HTTP/1.1 401 Unauthorized

{"detail":"Invalid webhook"}
```

A retry of the provider has the same `webhook-id` and a new timestamp and signature. It goes
through, and the handler, which has seen `msg_1` already, does nothing:

```console
$ sign && curl -i http://127.0.0.1:8000/webhooks/payments -H @signed.txt -d "$BODY"
HTTP/1.1 204 No Content
```

Registering an endpoint sends it a signed `ping`. A URL that isn't on the public internet is
refused before anything is sent: the cloud's metadata service, which hands out credentials,
the server itself, or a port other than 80 and 443:

```console
$ curl -i http://127.0.0.1:8000/endpoints -H 'content-type: application/json' -d '{"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}'
HTTP/1.1 422 Unprocessable Content

{"detail":"Not allowed: http://169.254.169.254: its address isn't allowed"}
$ curl -i http://127.0.0.1:8000/endpoints -H 'content-type: application/json' -d '{"url": "http://localhost/admin"}'
HTTP/1.1 422 Unprocessable Content

{"detail":"Not allowed: http://localhost: its address isn't allowed"}
$ curl -i http://127.0.0.1:8000/endpoints -H 'content-type: application/json' -d '{"url": "http://localhost:6379/"}'
HTTP/1.1 422 Unprocessable Content

{"detail":"Not allowed: http://localhost:6379: port 6379 isn't allowed"}
```

A public URL that answers the ping with a 2xx is registered, and the answer carries its secret
(`whsec_...`), shown once. Its deliveries are listed at `/endpoints/{id}/deliveries`.

## What to look at

- **The signature is checked before the body is parsed:** the endpoint takes the
  `WebhookMessage` and validates `message.body` itself. A pydantic body parameter would make
  FastAPI read and parse the whole body before the dependency runs, signed or not.
- **Replays and retries are different things:** `FastAPIWebhooks` refuses a delivery it has seen
  (same id and timestamp), in every process that shares the backend. A retry is a new delivery
  of the same message, so the handler is idempotent on `message.id`: in a database, a unique
  constraint on a row written in the same transaction as the order.
- **One secret per endpoint:** each user gets their own, made by
  `WebhookSigner.generate_secret()`, so one leaked secret doesn't forge events for the others.
  The exact bytes sent are the ones signed, and every attempt of an event keeps its id.
- **Checked when sent, not only when registered:** `SSRFGuardTransport` resolves the host on
  every call and connects to the address it checked, so a name that later points to
  `10.0.0.7` (or answers DNS differently the second time) gets a `blocked` delivery, not a
  request to the private network.
- **The guard inside `ResilientTransport`:** the 10-second deadline covers the DNS lookup, and
  each origin (scheme, host and port) has its own breaker: after five failures in a row it's
  skipped for a minute instead of being waited for. Responses are read up to 64 KiB, and redirects aren't followed.
- **After the response:** deliveries run as a background task, all endpoints at once, so the
  provider gets its 204 without waiting for the users' servers. A real app hands them to a task
  queue that retries failures.

## The code

<!-- fmt: off -->
```python title="examples/webhooks/main.py"
--8<-- "examples/webhooks/main.py"
```
<!-- fmt: on -->
