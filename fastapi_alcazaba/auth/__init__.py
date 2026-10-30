"""Authentication strategies, API keys stored as digests, revoked tokens and
one-time codes."""

from fastapi_alcazaba.auth.apikeys import APIKeyRecord, FastAPIAPIKeys
from fastapi_alcazaba.auth.strategies import (
    AuthenticationFailed,
    FastAPIAuth,
    HTTPBearerStrategy,
    SessionStrategy,
    Strategy,
)
from fastapi_alcazaba.auth.tokens import OneTimeCodes, TokenBlocklist

__all__ = [
    "APIKeyRecord",
    "AuthenticationFailed",
    "FastAPIAPIKeys",
    "FastAPIAuth",
    "HTTPBearerStrategy",
    "OneTimeCodes",
    "SessionStrategy",
    "Strategy",
    "TokenBlocklist",
]
