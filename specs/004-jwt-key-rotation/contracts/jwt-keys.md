# Contract: `JWTKey` and `JWTStrategy`

```python
from datetime import UTC, datetime

from fastapi_alcazaba.auth.strategies.jwt import JWKSUnavailable, JWTKey, JWTStrategy

tokens = JWTStrategy(
    keys=[
        JWTKey(settings.jwt_private_key, algorithm="ES256"),  # signs
        JWTKey(
            settings.old_private_key,
            algorithm="RS256",
            until=datetime(2026, 12, 1, tzinfo=UTC),
        ),
    ]
)
tokens.issue("ana")  # header {"alg": "ES256", "kid": "<thumbprint>"}
tokens.jwks()  # {"keys": [{"kty": "EC", ..., "kid": ..., "alg": "ES256", "use": "sig"}, ...]}

idp = JWTStrategy(
    jwks_url="https://idp.example.com/.well-known/jwks.json",
    algorithms=["RS256"],
    audience="api",
    issuer="https://idp.example.com/",
    jwks_timeout=3,
    jwks_lifespan=300,
    jwks_stale=3600,
)
await idp.aclose()  # at shutdown
```

| `JWTKey` parameter | Default | Meaning |
| --- | --- | --- |
| `key` | | an HMAC secret (32 bytes at least), a private key (PEM or object) or a public key |
| `algorithm` | | HS256/384/512, RS*, PS*, ES*, EdDSA |
| `kid` | derived | the key's id |
| `until` | None | an aware datetime; past it the key verifies nothing |

New `JWTStrategy` parameters: `keys`, `jwks_timeout` (3), `jwks_stale` (3600), `jwks_client`.
`algorithms` defaults to None: HS256 for `key=`; RS256, ES256, PS256 and EdDSA for `jwks_url`
(OIDCStrategy's); with `keys=` it can't be given, each key has its own.

`JWKSUnavailable` (an Exception): `decode` raises it when the provider's keys can't be had;
`authenticate` turns it into a 503 (WebSocket 1013).

Deprecated: `signing_key=` (`DeprecationWarning`): `keys=[JWTKey(private_key, algorithm=...)]`.
