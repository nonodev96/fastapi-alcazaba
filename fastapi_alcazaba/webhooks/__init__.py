"""Webhooks: verifying those received (Standard Webhooks, GitHub, Stripe, Slack), replays
refused, and signing and sending those the app sends.

`WebhookSender` and its types need the `webhooks` extra: they're imported when first used.
"""

from typing import TYPE_CHECKING, Any

from fastapi_alcazaba.webhooks.signing import FastAPIWebhooks, WebhookMessage, WebhookSigner

if TYPE_CHECKING:
    from fastapi_alcazaba.webhooks.sender import (
        EndpointHealth,
        WebhookDelivery,
        WebhookDeliveryFailed,
        WebhookEndpoint,
        WebhookSender,
    )

_LAZY = {
    "EndpointHealth",
    "WebhookDelivery",
    "WebhookDeliveryFailed",
    "WebhookEndpoint",
    "WebhookSender",
}

__all__ = [
    "EndpointHealth",
    "FastAPIWebhooks",
    "WebhookDelivery",
    "WebhookDeliveryFailed",
    "WebhookEndpoint",
    "WebhookMessage",
    "WebhookSender",
    "WebhookSigner",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from fastapi_alcazaba.webhooks import sender

        return getattr(sender, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
