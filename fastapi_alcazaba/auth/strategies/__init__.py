"""Authentication by strategies: `FastAPIAuth` and the strategies.

JWTStrategy, OAuth2Strategy, OIDCStrategy, SAMLStrategy and UnkeyStrategy need their extras
(`auth-strategy-jwt`, `auth-strategy-oauth2`, `auth-strategy-oidc`, `auth-strategy-saml`,
`auth-strategy-unkey`): they're imported when they're first used. Unkey's client and key
management (`UnkeyClient`, `UnkeyKeys`) are in `fastapi_alcazaba.auth.strategies.unkey`.
"""

from typing import TYPE_CHECKING, Any

from fastapi_alcazaba.auth.strategies.base import (
    AuthenticationFailed,
    FastAPIAuth,
    Strategy,
    granted_scopes,
)
from fastapi_alcazaba.auth.strategies.bearer import HTTPBearerStrategy
from fastapi_alcazaba.auth.strategies.session import SessionStrategy

if TYPE_CHECKING:
    from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy
    from fastapi_alcazaba.auth.strategies.oauth2 import OAuth2Strategy
    from fastapi_alcazaba.auth.strategies.oidc import OIDCStrategy
    from fastapi_alcazaba.auth.strategies.saml import SAMLProfile, SAMLStrategy
    from fastapi_alcazaba.auth.strategies.unkey import UnkeyStrategy

_LAZY = {
    "JWTStrategy": "fastapi_alcazaba.auth.strategies.jwt",
    "OAuth2Strategy": "fastapi_alcazaba.auth.strategies.oauth2",
    "OIDCStrategy": "fastapi_alcazaba.auth.strategies.oidc",
    "SAMLProfile": "fastapi_alcazaba.auth.strategies.saml",
    "SAMLStrategy": "fastapi_alcazaba.auth.strategies.saml",
    "UnkeyStrategy": "fastapi_alcazaba.auth.strategies.unkey",
}

__all__ = [
    "AuthenticationFailed",
    "FastAPIAuth",
    "HTTPBearerStrategy",
    "JWTStrategy",
    "OAuth2Strategy",
    "OIDCStrategy",
    "SAMLProfile",
    "SAMLStrategy",
    "SessionStrategy",
    "Strategy",
    "UnkeyStrategy",
    "granted_scopes",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
