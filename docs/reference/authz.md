# Authorization

A policy per route (`FastAPIAuthz`) or for every request (`AuthzMiddleware`), held by Casbin,
Cedar (`CedarEnforcer`) or Open Policy Agent (`OPAEnforcer`).
In `fastapi_alcazaba.authz`.

In the guide: [Authorization](../guide/authorization/index.md). In an app: [the examples](../examples/authorization/index.md).

::: fastapi_alcazaba.authz.FastAPIAuthz

::: fastapi_alcazaba.authz.AuthzMiddleware

::: fastapi_alcazaba.authz.cedar.CedarEnforcer

::: fastapi_alcazaba.authz.opa.OPAEnforcer

::: fastapi_alcazaba.authz.PolicyUnavailable

::: fastapi_alcazaba.authz.starlette_user

::: fastapi_alcazaba.authz.subject_id

::: fastapi_alcazaba.authz.Enforcer

::: fastapi_alcazaba.authz.WEBSOCKET
