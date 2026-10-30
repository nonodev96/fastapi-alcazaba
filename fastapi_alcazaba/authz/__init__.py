"""Authorization with Casbin: a dependency per route, or a middleware for every request."""

from fastapi_alcazaba.authz.enforcer import (
    WEBSOCKET,
    CasbinMiddleware,
    Enforcer,
    FastAPICasbin,
    starlette_user,
)

__all__ = [
    "WEBSOCKET",
    "CasbinMiddleware",
    "Enforcer",
    "FastAPICasbin",
    "starlette_user",
]
