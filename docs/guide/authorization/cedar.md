# Cedar

!!! warning "Experimental"
    New in 0.6: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../../features/scope.md) lists the tools that are settled.

Who may do what, in [Cedar](https://www.cedarpolicy.com)'s policy language, evaluated in the
process by `CedarEnforcer` for `FastAPIAuthz` and `AuthzMiddleware`. The `cedar` extra installs
[cedarpy](https://github.com/k9securityio/cedar-py), the bindings of Cedar's engine.

In an app: [the example](../../examples/authorization/cedar.md). How the dependency and the middleware work: [Authorization](index.md). Every class and function: [the API reference](../../reference/authz.md#fastapi_alcazaba.authz.cedar.CedarEnforcer).

A Cedar policy permits or forbids a principal an action on a resource, with conditions on their
attributes; what no policy permits is denied, and a `forbid` beats every `permit`:

```cedar title="policies.cedar"
// The members of a document's team read it.
permit (principal, action == Action::"read", resource)
when { principal.team == resource.team };

// Its owner reads and edits it.
permit (principal, action in [Action::"read", Action::"edit"], resource)
when { resource.owner == principal };

// Admins delete documents.
permit (principal in Role::"admin", action == Action::"delete", resource == Resource::"documents");

// Contractors never see a confidential document, whatever else allows it.
forbid (principal, action, resource)
when { principal.contractor && resource is Document && resource.confidential };
```

```python
from fastapi_alcazaba.authz import FastAPIAuthz
from fastapi_alcazaba.authz.cedar import CedarEnforcer

enforcer = CedarEnforcer(
    Path("policies.cedar").read_text(),
    entities=[{"uid": {"type": "Role", "id": "admin"}, "attrs": {}, "parents": []}],
    schema=Path("schema.cedarschema").read_text(),  # the policies are validated against it
)
authz = FastAPIAuthz(enforcer, subject=current_user)  # returns the user's entity


@app.get("/documents/{document_id}")
async def read_document(document_id: int, user: SignedIn) -> Document:
    document = await load_document(document_id)
    if document is None or not await authz.enforce(user, document_entity(document), "read"):
        raise HTTPException(404, "No such document")
    return document
```

- **The requests:** `FastAPIAuthz`'s (subject, object, action) become Cedar's (principal,
  action, resource). A name is an entity of `principal_type` (`User`), `action_type` (`Action`)
  or `resource_type` (`Resource`): `Depends(authz)` asks whether `User::"ana"` may
  `Action::"PUT"` the resource `Resource::"/articles/7"`, and `authz.require("documents",
  "delete")` about `Resource::"documents"`. Each resource named so gets an `id` attribute, its
  name, so that a policy matches paths: `when { resource.id like "/articles/*" }`.
- **Entities:** what the policies read about the principal and the resource. `entities=` are
  those of every request (roles and their hierarchy, resources with their attributes).
  `enforce` also takes a whole entity instead of a name (`{"uid": ..., "attrs": ...,
  "parents": [...]}`): the user your authentication loaded, with its roles as parents, and the
  row the route just read. `entities_of(subject)` adds others for each request: a plain
  function for what's at hand, an `async def` for what it reads from the database. An entity
  given twice (in `entities=` and by the route) must be the same both times, or `enforce`
  raises ValueError, a 500: neither copy wins, as a `forbid` would depend on which.
- **`context`:** `await authz.enforce(user, resource, "deploy", {"hour": now.hour})` gives the
  policies `context.hour`.
- **A schema** (`schema=`, in Cedar's language or JSON) validates the policies when the enforcer
  is made: a misspelled attribute or an action that doesn't apply to a type raises ValueError at
  start, not a wrong answer later. The entities are checked against it too. Invalid policies
  raise too.
- **Fails closed:** a policy that can't be evaluated (an attribute an entity doesn't have, an
  entity that doesn't exist) refuses the request, with a warning that says why. Cedar on its own
  skips such a policy, and a `forbid` that fails would let the request through.
- **Several processes:** each one evaluates its own copy of the policies, made at start: to
  change them, deploy them, or make a new `CedarEnforcer` when they change.
