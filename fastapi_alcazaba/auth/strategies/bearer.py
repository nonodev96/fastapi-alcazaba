"""Opaque tokens in `Authorization: Bearer`, which your function looks up. It needs no other
library: the `auth-strategy-http-bearer` extra installs nothing more.

    bearer = auth.use(HTTPBearerStrategy(find_token))

    async def find_token(token: str) -> User | None:  # your database, the provider's introspection
        digest = hashlib.sha256(token.encode()).hexdigest()  # store digests, not tokens
        return await db.user_by_token_digest(digest)

For JWTs, which carry their claims and a signature, JWTStrategy verifies them itself.
"""

import re
from collections.abc import Callable
from typing import Any

from fastapi.security import HTTPBearer
from starlette.requests import HTTPConnection

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, Strategy, call

#: The longest token read from a request.
MAX_TOKEN = 4_096
#: RFC 6750's b64token.
_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
#: What can go between the quotes of the challenge's realm.
_REALM = re.compile(r"[ !#-\[\]-~]+")


class HTTPBearerStrategy(Strategy):
    """Authenticates by the token of `Authorization: Bearer <token>`, which `verify` turns into
    the user. A token that isn't RFC 6750's syntax, or longer than 4 KiB, fails without reaching
    `verify`.

    :param verify: `verify(token)`: the user, or None when the token is unknown or expired (then
        authentication fails). Async, or a plain function, which runs in a thread. Keep digests
        of the tokens, not the tokens: `FastAPIAPIKeys` does it for API keys.
    :param realm: the realm of the 401's challenge, `Bearer realm="..."`.
    :param name: what `FastAPIAuth.authenticate` calls it by.
    """

    def __init__(
        self,
        verify: Callable[[str], Any],
        *,
        realm: str | None = None,
        name: str = "bearer",
    ) -> None:
        super().__init__(name)
        if realm is not None and not _REALM.fullmatch(realm):
            raise ValueError("the realm is printable ASCII, without quotes or backslashes")
        self.verify = verify
        self.challenge = f'Bearer realm="{realm}"' if realm is not None else "Bearer"

    def openapi_scheme(self) -> HTTPBearer:
        return HTTPBearer(scheme_name=self.name, auto_error=False)

    async def authenticate(self, connection: HTTPConnection) -> Any:
        scheme, _, token = connection.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = token.strip()
        if len(token) > MAX_TOKEN or not _TOKEN.fullmatch(token):
            raise AuthenticationFailed("malformed token")
        user = await call(self.verify, token)
        if user is None:
            raise AuthenticationFailed("unknown token")
        return user
