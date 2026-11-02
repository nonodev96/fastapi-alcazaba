# Authorization

--8<-- "docs/examples/animations/authorization.svg"

A policy decides who may do what, checked by [`FastAPIAuthz`](../../guide/authorization/index.md)
for each route or each action, and by `AuthzMiddleware` for every request, whatever engine holds
the policy:

- **In process**, with the policy in the app's memory:
    - [Casbin](casbin.md): a newsroom whose roles write, edit, publish and grant roles.
    - [Cedar](cedar.md): a team's documents, read and edited as their attributes allow, where
      contractors never see a confidential one.
- **[As a separate service](service.md)**, one policy decision point that every service asks:
    - [Open Policy Agent](opa.md): a shop whose orders and refunds a Rego policy decides, by
      the user's roles and the order's total.

Each example runs with uvicorn and its own users, tokens and data; the commands in its page show
what each user gets.
