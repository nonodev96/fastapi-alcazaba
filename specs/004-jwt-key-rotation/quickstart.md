# Quickstart: checking it end to end

```bash
.venv/bin/pytest tests/unit/test_jwt_keys.py tests/unit/test_auth_strategies.py -k "jwt or JWT"
```

By hand:

1. Make a strategy with `keys=[JWTKey(old_rsa, algorithm="RS256")]`, issue a token.
2. Make one with `keys=[JWTKey(new_ec, algorithm="ES256"), JWTKey(old_rsa, algorithm="RS256",
   until=<in a minute>)]`: the old token verifies; a new token's header has the EC key's `kid`.
3. After the minute, the old token is refused.
4. `jwks()` lists both public keys.
