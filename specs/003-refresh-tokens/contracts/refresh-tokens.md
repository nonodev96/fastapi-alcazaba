# Contract: `RefreshTokens`

```python
from fastapi_alcazaba.auth import RefreshRefused, RefreshTokens, TokenBlocklist, TokenPair
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy

jwt = auth.use(JWTStrategy(settings.jwt_secret, blocklist=TokenBlocklist()))
refresh = RefreshTokens(jwt, secret=settings.refresh_secret)

pair: TokenPair = await refresh.issue(user.id, client="ios", scope="read")  # at login
pair = await refresh.exchange(pair.refresh_token, client="ios")  # RefreshRefused → 401
await refresh.revoke(pair.refresh_token)  # logout here
await refresh.revoke_session(claims["sid"])  # an operator ends one login
await refresh.revoke_subject(user.id)  # everywhere
```

| Constructor parameter | Default | Meaning |
| --- | --- | --- |
| `strategy` | | the `JWTStrategy` that signs the access tokens; must be able to sign and have a `blocklist` |
| `secret` | | at least 32 bytes; a list rotates |
| `cache` | FastAPICache's | where families live; its backend must hold locks |
| `access_ttl` | 900 | seconds of each access token |
| `lifetime` | 30 days | seconds a family lasts from the login |
| `idle` | 7 days | seconds a family lasts without an exchange |
| `grace` | 10 | seconds the previous refresh token still gives the current one (0–60) |
| `namespace` | `"refresh"` | part of the keys |
| `audit` | None | a `FastAPIAudit` |

`TokenPair`: `access_token`, `refresh_token`, `token_type` ("bearer"), `expires_in` (access),
`refresh_expires_in` (seconds until the family's absolute end), `session` (the `sid`).

`RefreshRefused`: an `AuthenticationFailed` with `reason`.

`TokenBlocklist`: `revoke_session(session, *, ttl)`; `is_revoked(..., session=None)`.

`JWTStrategy`: refuses a token whose `sid` the blocklist revoked.
