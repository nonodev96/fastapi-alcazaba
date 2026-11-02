"""Authentication strategies, API keys stored as digests, revoked tokens, one-time codes, and
a second factor (TOTP, recovery codes, step-up), and passkeys (WebAuthn)."""

from fastapi_alcazaba.auth.apikeys import APIKeyRecord, FastAPIAPIKeys
from fastapi_alcazaba.auth.strategies import (
    AuthenticationFailed,
    FastAPIAuth,
    HTTPBearerStrategy,
    SessionStrategy,
    Strategy,
    granted_scopes,
)
from fastapi_alcazaba.auth.tokens import OneTimeCodes, TokenBlocklist
from fastapi_alcazaba.auth.totp import FastAPITOTP, RecoveryCodes, TOTPEnrollment
from fastapi_alcazaba.auth.webauthn import FastAPIWebAuthn, Passkey, WebAuthnRefused, credential_id

__all__ = [
    "APIKeyRecord",
    "AuthenticationFailed",
    "FastAPIAPIKeys",
    "FastAPIAuth",
    "FastAPITOTP",
    "FastAPIWebAuthn",
    "HTTPBearerStrategy",
    "OneTimeCodes",
    "Passkey",
    "RecoveryCodes",
    "SessionStrategy",
    "Strategy",
    "TOTPEnrollment",
    "TokenBlocklist",
    "WebAuthnRefused",
    "credential_id",
    "granted_scopes",
]
