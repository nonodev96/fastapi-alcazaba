"""What every tool shares: signing and encrypting what's stored, RFC 8785 canonical JSON,
the backends' circuit breaker and timeouts, and the exceptions."""

from fastapi_alcazaba.core.canonical import canonical_json
from fastapi_alcazaba.core.encryption import Encrypter
from fastapi_alcazaba.core.errors import (
    BackendUnavailable,
    CircuitOpen,
    UnsignedEntriesWarning,
    UnstableCacheKeyWarning,
)
from fastapi_alcazaba.core.integrity import Secret, Secrets, Signer
from fastapi_alcazaba.core.resilience import BackendGuard, CircuitBreaker

__all__ = [
    "BackendGuard",
    "BackendUnavailable",
    "canonical_json",
    "CircuitBreaker",
    "CircuitOpen",
    "Encrypter",
    "Secret",
    "Secrets",
    "Signer",
    "UnsignedEntriesWarning",
    "UnstableCacheKeyWarning",
]
