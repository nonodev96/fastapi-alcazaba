# Your own strategy

An API key, a client certificate, a signed request…: any way to tell who makes a request.

In an app: [the example](../../examples/auth_strategies/your-own.md). How strategies are tried: [Strategies](index.md#strategies). Every argument: [the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.Strategy).

Your own strategy (an API key, a client certificate, a signed request…) is a `Strategy` whose
`authenticate()` returns the user, None, or raises `AuthenticationFailed`; `challenge` and
`openapi_scheme()` are optional:

```python
from fastapi import HTTPException
from starlette.requests import HTTPConnection

from fastapi_alcazaba.auth import AuthenticationFailed, FastAPIAPIKeys, Strategy


class APIKeyStrategy(Strategy):
    challenge = "APIKey"

    def __init__(self, keys: FastAPIAPIKeys[Any]) -> None:
        super().__init__("api-key")
        self.keys = keys

    async def authenticate(self, connection: HTTPConnection) -> Any:
        key = connection.headers.get("x-api-key")
        if key is None:
            return None  # not this strategy's: the next one is tried
        try:
            return await self.keys.authenticate(key)
        except HTTPException as exc:  # FastAPIAPIKeys' 401
            raise AuthenticationFailed("invalid API key") from exc


auth.use(APIKeyStrategy(keys))
```
