# Casbin

Who may do what, in a [Casbin](https://casbin.org) model and policy (ACL, RBAC, RBAC with
domains, ABAC…), checked in the process by `FastAPIAuthz` and `AuthzMiddleware`.

In an app: [the example](../../examples/authorization/casbin.md). How the dependency and the middleware work: [Authorization](index.md). Every class and function: [the API reference](../../reference/authz.md).

## Casbin policies

The `casbin` extra installs [PyCasbin](https://github.com/casbin/pycasbin), and its enforcers go to
`FastAPIAuthz` as they are.

A Casbin model says what a request is and how it matches the policy; the policy, who may do
what. Here a request is (user, object, action), and users get permissions through roles:

```ini title="model.conf"
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub) && keyMatch2(r.obj, p.obj) && r.act == p.act
```

```text title="policy.csv"
p, reader, /articles/:id, GET
p, editor, /articles/:id, PUT
p, editor, articles, publish
g, editor, reader
g, ana, editor
```

```python
import casbin

from fastapi_alcazaba.authz import FastAPIAuthz

enforcer = casbin.Enforcer("model.conf", "policy.csv")
authz = FastAPIAuthz(enforcer, subject=current_username)  # your dependency: who's asking

# (user, path, method): may ana PUT /articles/7?
api = APIRouter(dependencies=[Depends(authz)])


# (user, *values): your own objects and actions. Both dependencies return the user.
@app.post("/articles/{article_id}/publish")
async def publish(
    article_id: int,
    editor: Annotated[str, Depends(authz.require("articles", "publish"))],
) -> Article: ...


# {name} takes a path parameter: (user, "projects/7", "archive").
archive = authz.require("projects/{project_id}", "archive")


@app.post("/projects/{project_id}/archive", dependencies=[Depends(archive)])
async def archive_project(project_id: int) -> None: ...
```

- **Names in `keyMatch2`:** `:name` is a parameter, so name objects without colons
  (`projects/7`, not `project:7`), or the rule for `project:7` lets `project:8` through.
- **Visitors:** with `anonymous="*anonymous"`, `p, *anonymous, /catalog, GET` opens a route to
  everybody.
- **Fails closed:** a broken model, or a request that doesn't fit it (`invalid request size`),
  raises: a 500, never a request let through. A disabled enforcer
  (`enforcer.enable_enforce(False)`) lets everything through, as Casbin does.
- **Enforcers:** any of Casbin's: `Enforcer`, `SyncedEnforcer`, `FastEnforcer`, `AsyncEnforcer`
  (for async adapters; `await enforcer.load_policy()` in the lifespan). The checks run in the
  event loop, like an AsyncEnforcer's changes: a request never sees half a change. Casbin tries
  the rules one by one, a few microseconds each (about 2.5 ms per request with 1,000 rules on a
  laptop): give permissions to roles rather than users, and roles to users.
- **Several processes:** each one has its copy of the policy. A change made in one
  (`await enforcer.add_role_for_user(...)`) reaches the database through the adapter, and the
  other processes when they load the policy again: a Casbin watcher (casbin-redis-watcher…)
  tells them.
