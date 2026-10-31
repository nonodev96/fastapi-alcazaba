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
- **OpenAPI:** it shows in the schema, for the docs' Authorize button.
