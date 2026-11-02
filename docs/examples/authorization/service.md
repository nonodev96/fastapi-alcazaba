# As a separate service

--8<-- "docs/examples/animations/authz_service.svg"

A policy decision point that runs apart from the app, which every service asks over the network:
Open Policy Agent, OpenFGA, SpiceDB, Cerbos. The policies are deployed and audited in one place,
for every service at once.

The guide tells [what changes](../../guide/authorization/service.md) from an engine in the
process: the latency of each check, a 503 when the service doesn't answer, and an enforcer that
awaits the answer.

- [Open Policy Agent](opa.md): a shop whose orders and refunds a Rego policy decides, by the
  user's roles and the order's total, and which refuses everything while OPA is down.

Without a network, the engines in the process: [Casbin](casbin.md) and [Cedar](cedar.md).
