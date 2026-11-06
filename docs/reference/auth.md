# API keys, tokens and codes

Passwords, API keys stored as digests, revoked tokens, one-time codes, the second factor (TOTP),
passkeys (WebAuthn) and step-up.
In `fastapi_alcazaba.auth`.

In the guide: [Passwords](../guide/passwords.md), [API keys](../guide/auth.md#api-keys), [Revoked tokens and one-time codes](../guide/auth.md#revoked-tokens-and-one-time-codes), [Second factor](../guide/two-factor.md), [Passkeys](../guide/passkeys.md). In an app: [the example](../examples/auth.md), [the passwords'](../examples/passwords.md), [the second factor's](../examples/two_factor.md).

::: fastapi_alcazaba.auth.FastAPIPasswords

::: fastapi_alcazaba.auth.ScryptParameters

::: fastapi_alcazaba.auth.PasswordCheck

::: fastapi_alcazaba.auth.PasswordStrength

::: fastapi_alcazaba.auth.PasswordBlocklist

::: fastapi_alcazaba.auth.PwnedPasswords

::: fastapi_alcazaba.auth.FastAPIAPIKeys

::: fastapi_alcazaba.auth.APIKeyRecord

::: fastapi_alcazaba.auth.TokenBlocklist

::: fastapi_alcazaba.auth.OneTimeCodes

::: fastapi_alcazaba.auth.FastAPITOTP

::: fastapi_alcazaba.auth.TOTPEnrollment

::: fastapi_alcazaba.auth.RecoveryCodes

::: fastapi_alcazaba.auth.FastAPIWebAuthn

::: fastapi_alcazaba.auth.Passkey

::: fastapi_alcazaba.auth.WebAuthnRefused

::: fastapi_alcazaba.auth.credential_id

::: fastapi_alcazaba.auth.stepup.mark

::: fastapi_alcazaba.auth.stepup.verified_at

::: fastapi_alcazaba.auth.stepup.require_recent
