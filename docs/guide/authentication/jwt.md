# JWTStrategy

JWTs, the app's own or an identity provider's: `pip install "fastapi-alcazaba[auth-strategy-jwt]"`.

In an app: [the example](../../examples/auth_strategies/jwt.md). How strategies are tried: [Strategies](index.md#strategies). Every argument: [the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.strategies.jwt.JWTStrategy).

`JWTStrategy` reads the token of `Authorization: Bearer` (or of the cookie named by `cookie=`)
and verifies it with PyJWT: the signature, `exp`, `nbf` and the `required` claims (`exp`, `sub`),
and `aud` and `iss` when given; a token with an `aud` is refused when no `audience` is set, as
it's meant for another service. `alg: none` never, nor HMAC and asymmetric algorithms together: a
public key taken for an HMAC secret would let anyone sign.

```python
from fastapi_alcazaba.auth import TokenBlocklist
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy

# The app's own tokens, signed with the first secret; the second one still verifies.
tokens = auth.use(
    JWTStrategy(
        [settings.jwt_secret, settings.previous_jwt_secret],
        verify=lambda claims: load_user(claims["sub"]),
        blocklist=TokenBlocklist(),
    )
)
token = tokens.issue(str(user.id), expires_in=900)

# An identity provider's tokens, with its public keys.
idp = auth.use(
    JWTStrategy(
        jwks_url="https://idp.example.com/.well-known/jwks.json",
        algorithms=["RS256"],
        audience="https://api.example.com",
        issuer="https://idp.example.com/",
        name="idp",
    )
)
```

- **Keys:** HMAC secrets have at least 32 bytes and rotate with a list; public keys need
  `algorithms=["RS256"]` (or ES256, EdDSA…), and an identity provider's keys come from its JWKS
  (`jwks_url=`, by each token's `kid`), with the `audience` and `issuer` it requires: a provider
  signs the tokens of every app it serves with the same keys.
- **Revoked tokens:** `blocklist=` refuses the tokens revoked in a `TokenBlocklist` (see "Revoked
  tokens and one-time codes").
- **Issuing:** `issue(subject, expires_in=900)` signs one, with `iat`, `exp` and a random `jti`.
- **Scopes:** the token grants those of its `scope` (a string separated by spaces) and `scp` (a
  list) claims, for the routes that need some ([Scopes](index.md#scopes)); `scope_claims=`
  names others. A claim that isn't scopes refuses the token.
- **OpenAPI:** it shows in the schema, for the docs' Authorize button.

## Refresh tokens

Access tokens should be short (15 minutes), but users shouldn't log in every 15 minutes.
`RefreshTokens` gives each login a refresh token as well, which gets a new pair when the access
token expires:

```python
from fastapi import HTTPException

from fastapi_alcazaba.auth import RefreshRefused, RefreshTokens, TokenBlocklist, TokenPair
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy

tokens = auth.use(JWTStrategy(settings.jwt_secret, blocklist=TokenBlocklist()))
refresh_tokens = RefreshTokens(tokens, secret=settings.refresh_secret)


@app.post("/token")
async def log_in(form: Login) -> TokenPair:
    user = await check_password(form)
    return await refresh_tokens.issue(user.id, scope="read write")


@app.post("/token/refresh")
async def refresh(body: Refresh) -> TokenPair:
    try:
        return await refresh_tokens.exchange(body.refresh_token)
    except RefreshRefused:
        raise HTTPException(401, "Log in again") from None  # never say why


@app.delete("/token", status_code=204)
async def log_out(claims: Annotated[dict[str, Any], Depends(auth.authenticate("jwt"))]) -> None:
    await refresh_tokens.revoke_session(claims["sid"])
```

- **Rotation:** each exchange gives a new pair and uses the refresh token up. A login (a
  *family*) has an id, the access tokens' `sid` claim; its claims (`scope` above) go into every
  access token it gets.
- **A copied token ends the login:** a refresh token already exchanged that comes back was copied,
  by a thief or from the user. Whoever comes second, the login ends: its refresh token and its
  access tokens are refused, and it's logged, counted (`fastapi_alcazaba.refresh`, outcome
  `reused`) and audited. The user logs in again; the thief is out.
- **Grace:** within `grace` seconds (10) of an exchange, the token just exchanged gets the newest
  refresh token again, with a new access token: tabs or requests that refresh at once all end up
  with one token. After that, it's a copy.
- **Lifetimes:** a login lasts `lifetime` (30 days) from the password, and `idle` (7 days)
  without a refresh; `access_ttl` (900 s) is the access tokens'.
- **Clients:** `issue(..., client="ios")` binds the login to a client; an exchange must give the
  same `client=`, and a token presented by another one ends the login.
- **Logging out:** `revoke(refresh_token)` with the refresh token, `revoke_session(sid)` with an
  access token's `sid` (a list of the user's devices, an operator), `revoke_subject(user_id)`
  everywhere (after a password change; the login started during that second stays, for the
  session that changed it). The strategy's `TokenBlocklist` refuses the access tokens until they
  expire, so it must have one.
- **Nothing stored makes a token:** a refresh token is `r1.<family>.<generation>.<MAC>`, the MAC
  made with `secret`. The backend keeps one record per login, with a random salt, never a token:
  whoever reads it can't make one. Every generation can be checked, so a forged token naming a
  real family (its id is in every access token) is refused without ending anything. `secret` is a
  list to rotate it, as the strategy's key.
- **One exchange at a time:** each login's exchanges hold its lock, so the backend must hold locks
  (every shipped backend does). If the backend fails, nothing is issued: `BackendUnavailable`.
- **Where clients keep it:** a refresh token is a password for 30 days. Browsers: an `HttpOnly`,
  `Secure`, `SameSite=Strict` cookie scoped to the refresh route, not JavaScript's storage; apps:
  the keychain. That's up to the app; nothing here enforces it.
- **`RefreshRefused`** has a `reason` (`malformed`, `unknown`, `forged`, `expired`, `revoked`,
  `reused`, `wrong_client`) for your logs, never for the client.
