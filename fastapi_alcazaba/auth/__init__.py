"""Authentication strategies, API keys stored as digests, revoked tokens, one-time codes, and
passwords, a second factor (TOTP, recovery codes, step-up), and passkeys (WebAuthn)."""

from fastapi_alcazaba.auth.apikeys import APIKeyRecord, FastAPIAPIKeys
from fastapi_alcazaba.auth.passwords import FastAPIPasswords, PwnedPasswords, ScryptParameters
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
    "FastAPIPasswords",
    "FastAPITOTP",
    "FastAPIWebAuthn",
    "HTTPBearerStrategy",
    "OneTimeCodes",
    "Passkey",
    "PwnedPasswords",
    "RecoveryCodes",
    "ScryptParameters",
    "SessionStrategy",
    "Strategy",
    "TOTPEnrollment",
    "TokenBlocklist",
    "WebAuthnRefused",
    "credential_id",
    "granted_scopes",
]
