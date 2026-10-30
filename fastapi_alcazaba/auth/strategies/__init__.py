"""Authentication by strategies: `FastAPIAuth` and the strategies.

JWTStrategy, OAuth2Strategy and SAMLStrategy need their extras (`auth-strategy-jwt`,
`auth-strategy-oauth2`, `auth-strategy-saml`): they're imported when they're first used.
"""

from typing import TYPE_CHECKING, Any

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, FastAPIAuth, Strategy
from fastapi_alcazaba.auth.strategies.bearer import HTTPBearerStrategy
from fastapi_alcazaba.auth.strategies.session import SessionStrategy

if TYPE_CHECKING:
    from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy
    from fastapi_alcazaba.auth.strategies.oauth2 import OAuth2Strategy
    from fastapi_alcazaba.auth.strategies.saml import SAMLProfile, SAMLStrategy

_LAZY = {
    "JWTStrategy": "fastapi_alcazaba.auth.strategies.jwt",
    "OAuth2Strategy": "fastapi_alcazaba.auth.strategies.oauth2",
    "SAMLProfile": "fastapi_alcazaba.auth.strategies.saml",
    "SAMLStrategy": "fastapi_alcazaba.auth.strategies.saml",
}

__all__ = [
    "AuthenticationFailed",
    "FastAPIAuth",
    "HTTPBearerStrategy",
    "JWTStrategy",
    "OAuth2Strategy",
    "SAMLProfile",
    "SAMLStrategy",
    "SessionStrategy",
    "Strategy",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
