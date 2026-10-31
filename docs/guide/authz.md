# Authorization with Casbin

Who may do what to which resource, decided by a Casbin policy (ACL, RBAC, ABAC…) per route or for
every request, instead of checks spread over the routes.

In an app: [the example](../examples/authz.md). Every class and function: [the API reference](../reference/authz.md).

## Casbin policies

`FastAPICasbin` lets a request through only when a [Casbin](https://casbin.org) policy allows it.
Who may do what lives in the policy (ACL, RBAC, RBAC with domains, ABAC…), not in checks spread
over the routes. The `casbin` extra installs [PyCasbin](https://github.com/casbin/pycasbin).

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

from fastapi_alcazaba.authz import FastAPICasbin

enforcer = casbin.Enforcer("model.conf", "policy.csv")
authz = FastAPICasbin(enforcer, subject=current_username)  # your dependency: who's asking

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

- **Who:** `subject` is a FastAPI dependency that returns who makes the request: a user name, an
  id, or an object for ABAC matchers (`r.sub.age >= 18`); None when nobody is logged in. Its own
  dependencies (a bearer token, a session) appear in the OpenAPI schema, for the docs' Authorize
  button. Without one, the user of Starlette's `AuthenticationMiddleware`.
- **What:** `Depends(authz)` checks the path the app routes (without the `root_path` a proxy or
  a mount adds in front) and the method; WebSockets have the action `WEBSOCKET`, so a policy for
  GET doesn't open them. `authz.require(...)` checks your own values instead, `{name}` filled
  from the path parameters. In `keyMatch2`, `:name` is a parameter: name objects without colons
  (`projects/7`, not `project:7`), or the rule for `project:7` lets `project:8` through.
- **Answers:** a request without a subject gets a 401 with `WWW-Authenticate: Bearer`
  (`challenge=`), without asking the policy; `anonymous="*anonymous"` has the policy check it as
  that subject instead, for routes open to everybody (`p, *anonymous, /catalog, GET`): a name no
  user can have, or whoever signs up as it gets its rules. A subject
  that the policy refuses gets a 403. WebSockets are closed with 1008 (policy violation). A path
  with control characters (`/users/%0A`) gets a 400 without asking the policy: Starlette routes
  it to `/users/`, while the policy would read the newline. With `FastAPIProblemDetails`, the
  errors are problem details.
- **In a route:** `authz.enforce(user, "drafts", "read")` returns a bool, for what depends on the
  data: the items of a list, the owner of an object.
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

`CasbinMiddleware` checks every request instead, as (user, path, method) with the user of
Starlette's `AuthenticationMiddleware` (its `identity`), and refuses requests without one. Add
it before `AuthenticationMiddleware`, so that it runs after it. With `anonymous="*anonymous"`,
those are checked as that subject, and the policy opens the public routes (`/docs`, `/login`…)
to it:

```python
from starlette.middleware.authentication import AuthenticationMiddleware

from fastapi_alcazaba.authz import CasbinMiddleware

app.add_middleware(CasbinMiddleware, enforcer=enforcer, anonymous="*anonymous")
app.add_middleware(AuthenticationMiddleware, backend=JWTBackend())
```

It checks the path the app routes, without the `root_path`, and the method: GET for HEAD, which
runs the GET route, and `WEBSOCKET` for WebSockets. Refusals get a 403 with
`{"detail": "Forbidden"}`, like FastAPI's errors (problem details with `FastAPIProblemDetails`),
or a 401 with `WWW-Authenticate` when there's no user (or `anonymous` is refused), and a 400 for paths with control characters; WebSockets are closed with 1008. `OPTIONS`
requests are checked like the others: add `CORSMiddleware` after `CasbinMiddleware` (outside it),
to answer the preflights.
