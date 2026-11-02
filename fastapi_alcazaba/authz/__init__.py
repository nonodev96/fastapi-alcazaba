"""Authorization by a policy engine, Casbin's, Cedar's or Open Policy Agent's: a dependency per
route, or a middleware for every request. Cedar's enforcer is in `fastapi_alcazaba.authz.cedar`,
Open Policy Agent's in `fastapi_alcazaba.authz.opa`."""

from fastapi_alcazaba.authz.enforcer import (
    WEBSOCKET,
    AuthzMiddleware,
    Enforcer,
    FastAPIAuthz,
    PolicyUnavailable,
    starlette_user,
    subject_id,
)

__all__ = [
    "WEBSOCKET",
    "AuthzMiddleware",
    "Enforcer",
    "FastAPIAuthz",
    "PolicyUnavailable",
    "starlette_user",
    "subject_id",
]
