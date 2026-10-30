# Webhooks and outbound calls

Standard Webhooks in and out, and httpx transports with deadlines, breakers and SSRF checks.
In `fastapi_alcazaba.webhooks` and `fastapi_alcazaba.outbound`.

In the guide: [Signed webhooks](../guide/webhooks.md#signed-webhooks), [Outbound calls](../guide/webhooks.md#outbound-calls). In an app: [the example](../examples/webhooks.md).

::: fastapi_alcazaba.webhooks.FastAPIWebhooks

::: fastapi_alcazaba.webhooks.WebhookSigner

::: fastapi_alcazaba.webhooks.WebhookMessage

::: fastapi_alcazaba.outbound.ResilientTransport

::: fastapi_alcazaba.outbound.CircuitOpenError

::: fastapi_alcazaba.outbound.ResponseTooLargeError

::: fastapi_alcazaba.outbound.SSRFGuardTransport

::: fastapi_alcazaba.outbound.BlockedDestinationError
