# Open Policy Agent

!!! warning "Experimental"
    New in 0.6: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../../features/scope.md) lists the tools that are settled.

Who may do what, in [Open Policy Agent](https://www.openpolicyagent.org)'s policy language,
Rego, decided by an OPA that runs as a service of its own: `OPAEnforcer` asks it through its
REST API for `FastAPIAuthz` and `AuthzMiddleware`. The `opa` extra installs httpx2, which it
asks with.

In an app: [the example](../../examples/authorization/opa.md). What changes from an engine in the process: [As a separate service](service.md). Every class and function: [the API reference](../../reference/authz.md#fastapi_alcazaba.authz.opa.OPAEnforcer).

A Rego rule is true when every condition in one of its bodies is; `default` gives it a value
when none is:

```rego title="shop.rego"
package shop

# Nothing is allowed unless a rule below says so.
default allow := false

# Everybody reads the catalogue, signed in or not.
allow if {
	input.action == "GET"
	input.resource == "/products"
}

# The support staff refund up to 100 euros; above that, a manager does.
allow if {
	input.action == "refund"
	"support" in input.subject.roles
	input.resource.total <= 100
}

allow if {
	input.action == "refund"
	"manager" in input.subject.roles
}
```

```console
$ docker run -p 8181:8181 -v "$PWD:/policies:ro" openpolicyagent/opa:1.21.4 \
    run --server --addr 0.0.0.0:8181 /policies/shop.rego
```

```python
from fastapi_alcazaba.authz import FastAPIAuthz
from fastapi_alcazaba.authz.opa import OPAEnforcer

enforcer = OPAEnforcer(settings.opa_url, rule="shop.allow", token=settings.opa_token)
authz = FastAPIAuthz(enforcer, subject=current_user)  # returns the user, with its roles


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await enforcer.aclose()


@app.post("/orders/{order_id}/refund")
async def refund_order(order_id: int, user: SignedIn) -> Order:
    order = await load_order(order_id)
    if not await authz.enforce(user, order.model_dump(), "refund"):
        raise HTTPException(403, "Refunds are for the support staff, and a manager above 100 euros")
    ...
```

- **The query:** each check asks for `rule` (`shop.allow`, the rule `allow` of `package shop`)
  with the input `{"subject": ..., "resource": ..., "action": ..., "context": {...}}`:
  `POST /v1/data/shop/allow`. `Depends(authz)` sends the path and the method
  (`"/products"`, `"GET"`), `authz.require("orders", "export")` its values, and
  `authz.enforce(user, order, "refund")` whatever you give it, dicts with attributes too. Every
  value goes as JSON.
- **Only `true` allows:** `false` refuses, and so does a rule that is undefined for the input,
  with a warning: give it a default, `default allow := false`. A result that isn't a boolean (a
  `rule` that names a package, a rule that gives an object) raises RuntimeError, a 500.
- **Fails closed:** an OPA that doesn't answer within `timeout` (1 second), or answers with a
  5xx, raises `PolicyUnavailable`, and the request gets a 503. The enforcer's own client has a
  circuit breaker (`ResilientTransport`): after 5 failures in a row, requests are refused at
  once for 30 seconds instead of each waiting for OPA. A query OPA refuses (a 4xx: a wrong
  token) raises RuntimeError, a 500: a mistake to fix, not a refusal.
- **Where it runs:** next to the app, as a sidecar on `localhost`, so that each check stays
  under a millisecond. `client=` takes an httpx2 client of your own, for TLS to an OPA farther
  away or a transport of your own; `aclose()` only closes the enforcer's own.
- **A token:** with `opa run --authentication=token`, OPA asks for a bearer token: `token=`
  (a `SecretStr` from your settings). An empty one raises ValueError at start.
- **Changing the policy:** OPA loads it from files, bundles or its API, and every app that asks
  it sees the change at once, without a deploy of theirs.
