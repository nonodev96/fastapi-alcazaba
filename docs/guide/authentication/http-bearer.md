# HTTPBearerStrategy

Opaque bearer tokens that your function looks up:
`pip install "fastapi-alcazaba[auth-strategy-http-bearer]"`, which installs nothing more.

In an app: [the example](../../examples/auth_strategies/http-bearer.md). How strategies are tried: [Strategies](index.md#strategies). Every argument: [the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.HTTPBearerStrategy).

`HTTPBearerStrategy` takes opaque tokens of `Authorization: Bearer`, which your `verify(token)`
looks up (in a database, at the provider's introspection endpoint). It needs no other library.

```python
from fastapi_alcazaba.auth import HTTPBearerStrategy


async def find_token(token: str) -> User | None:
    digest = hashlib.sha256(token.encode()).hexdigest()  # store digests, not tokens
    return await db.user_by_token_digest(digest)


bearer = auth.use(HTTPBearerStrategy(find_token))
```

A token that isn't RFC 6750's syntax, or longer than 4 KiB, fails without reaching `verify`.
Keep digests of the tokens, not the tokens. Behind a JWTStrategy, JWTs go to that one and opaque
tokens to this one.
