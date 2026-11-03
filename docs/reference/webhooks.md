# Webhooks and outbound calls

Webhooks in (Standard Webhooks, GitHub, Stripe, Slack) and out, and httpx transports with deadlines, breakers and SSRF checks.
In `fastapi_alcazaba.webhooks` and `fastapi_alcazaba.outbound`.

In the guide: [Signed webhooks](../guide/webhooks.md#signed-webhooks), [Outbound calls](../guide/webhooks.md#outbound-calls). In an app: [the example](../examples/webhooks.md).

::: fastapi_alcazaba.webhooks.FastAPIWebhooks

::: fastapi_alcazaba.webhooks.WebhookSigner

::: fastapi_alcazaba.webhooks.WebhookMessage

::: fastapi_alcazaba.webhooks.sender.WebhookSender

::: fastapi_alcazaba.webhooks.sender.WebhookEndpoint

::: fastapi_alcazaba.webhooks.sender.WebhookDelivery

::: fastapi_alcazaba.webhooks.sender.EndpointHealth

::: fastapi_alcazaba.webhooks.sender.WebhookDeliveryFailed

::: fastapi_alcazaba.outbound.ResilientTransport

::: fastapi_alcazaba.outbound.CircuitOpenError

::: fastapi_alcazaba.outbound.ResponseTooLargeError

::: fastapi_alcazaba.outbound.SSRFGuardTransport

::: fastapi_alcazaba.outbound.BlockedDestinationError
