"""Outbound calls: httpx transports with a deadline per call, a circuit breaker per origin
and SSRF protection. Needs httpx."""

from fastapi_alcazaba.outbound.transports import (
    BlockedDestinationError,
    CircuitOpenError,
    ResilientTransport,
    ResponseTooLargeError,
    SSRFGuardTransport,
)

__all__ = [
    "BlockedDestinationError",
    "CircuitOpenError",
    "ResilientTransport",
    "ResponseTooLargeError",
    "SSRFGuardTransport",
]
