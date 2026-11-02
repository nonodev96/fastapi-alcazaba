# As a separate service

A policy decision point that runs apart from the app: every service of a company asks the same
one, over the network, and the policies are deployed and audited in one place.

How the dependency and the middleware work: [Authorization](index.md). The engines that run in
the process, with no network: [Casbin](casbin.md) and [Cedar](cedar.md).

| Service | Model | For |
| --- | --- | --- |
| [Open Policy Agent](opa.md) | Policies in Rego, any model | One policy language for the whole infrastructure (Kubernetes, gateways, services) |
| [OpenFGA](https://openfga.dev), [SpiceDB](https://authzed.com/spicedb) | Relationships, as Google's Zanzibar | Permissions that follow relationships: shared documents, organisations, teams |
| [Cerbos](https://www.cerbos.dev) | Policies in YAML, RBAC and ABAC | Readable policies with tests of their own |

What changes from an engine in the process:

- **Latency:** each check is a request over the network, a fraction of a millisecond next to
  the service, against a few microseconds in the process. Ask once per request, not once per
  item of a list.
- **Failures:** when the service doesn't answer, the request is refused, never let through. An
  enforcer raises `PolicyUnavailable`, and the request gets a 503 (`Authorization unavailable`),
  a WebSocket 1013 (try again later), counted as `unavailable` in the `fastapi_alcazaba.authz`
  metric. Each call has a deadline, and a circuit breaker stops asking a service that is down,
  so that requests don't each wait for it.
- **Async:** a call over the network can't block the event loop. `FastAPIAuthz` and
  `AuthzMiddleware` await an `enforce` that is a coroutine function.

[Open Policy Agent](opa.md) has an enforcer of its own, `OPAEnforcer`. For another service, an
enforcer is a class with an async `enforce`, which raises `PolicyUnavailable` when the service
doesn't answer:

```python
import httpx2 as httpx

from fastapi_alcazaba.authz import FastAPIAuthz, PolicyUnavailable
from fastapi_alcazaba.outbound import ResilientTransport


class PolicyService:
    def __init__(self, url: str) -> None:
        # A deadline for each call, and a circuit breaker for the service.
        self.client = httpx.AsyncClient(base_url=url, transport=ResilientTransport(timeout=0.5))

    async def enforce(self, subject: str, resource: str, action: str) -> bool:
        try:
            response = await self.client.post(
                "/check", json={"subject": subject, "resource": resource, "action": action}
            )
        except httpx.TransportError as exc:
            raise PolicyUnavailable(f"the policy service didn't answer: {exc!r}") from exc
        if response.status_code >= 500:
            raise PolicyUnavailable(f"the policy service answered {response.status_code}")
        response.raise_for_status()  # a 4xx is a mistake to fix: a 500
        return response.json()["allowed"] is True  # only true allows


enforcer = PolicyService("http://policy-service:8080")  # enforcer.client.aclose() at shutdown
authz = FastAPIAuthz(enforcer, subject=current_username)
```
