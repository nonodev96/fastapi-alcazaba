# Authorization

Who may do what to which resource, decided by a policy per route or for every request, instead
of checks spread over the routes.

In an app: [the examples](../../examples/authorization/index.md). Every class and function: [the API reference](../../reference/authz.md).

`FastAPIAuthz` is a dependency that lets a request through only when the policy allows it, and
`AuthzMiddleware` checks every request. Neither is tied to an engine: they take an enforcer,
anything with `enforce(subject, *values) -> bool`, or `async def enforce` for an engine that
answers through the network, which they await.

- **In process:** the engine runs in your app, with the policy in memory: no network, a few
  microseconds per check.
    - [Casbin](casbin.md): a model and a policy (ACL, RBAC, RBAC with domains, ABAC), its
      enforcers as they are. The `casbin` extra.
    - [Cedar](cedar.md): policies in Cedar's language, validated against a schema, with entities
      and their attributes; a `forbid` beats every `permit`. The `cedar` extra.
- **[As a separate service](service.md):** a policy decision point that the app asks over the
  network, shared by every service; when it doesn't answer, a 503.
    - [Open Policy Agent](opa.md): policies in Rego, queried through its REST API. The `opa`
      extra.

```python
from fastapi_alcazaba.authz import FastAPIAuthz

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

- **Who:** `subject` is a FastAPI dependency that returns who makes the request: a user name, an
  id, or what the engine reads attributes from (an object for Casbin's ABAC matchers, an entity
  for Cedar); None when nobody is logged in. Its own dependencies (a bearer token, a session)
  appear in the OpenAPI schema, for the docs' Authorize button. Without one, the user of
  Starlette's `AuthenticationMiddleware` (its `identity`).
- **What:** `Depends(authz)` checks the path the app routes (without the `root_path` a proxy or
  a mount adds in front) and the method: GET for HEAD, which runs the GET route, and
  `WEBSOCKET` for WebSockets, so a policy for GET doesn't open them. `authz.require(...)` checks
  your own values instead, `{name}` filled from the path parameters.
- **Answers:** a request without a subject gets a 401 with `WWW-Authenticate: Bearer`
  (`challenge=`), without asking the policy; `anonymous="*anonymous"` has the policy check it as
  that subject instead, for routes open to everybody: a name no user can have, or whoever signs
  up as it gets its rules. A subject that the policy refuses gets a 403. WebSockets are closed
  with 1008 (policy violation). A path with control characters (`/users/%0A`) gets a 400 without
  asking the policy: Starlette routes it to `/users/`, while the policy would read the newline.
  With `FastAPIProblemDetails`, the errors are problem details.
- **In a route:** `await authz.enforce(user, "drafts", "read")` returns a bool, for what
  depends on the data: the items of a list, the owner of an object.
- **Counted:** every decision in the `fastapi_alcazaba.authz` metric, by outcome (allowed,
  denied, unauthenticated, unavailable): a burst of denied is someone probing.
- **In the audit trail:** with `audit=` (a [`FastAPIAudit`](../audit.md)), each request the
  policy refuses is an `authz.denied` event (the 403s, and the 400s of paths with control
  characters), and each one the engine couldn't decide an `authz.unavailable`, with who asked
  (`actor=`, `subject_id` by default: an ID, a Cedar entity's `id`, a `sub`, `id` or `name`),
  what (the path, or the values of `require`), the route and the client. Not the 401s of
  requests without a subject, nor `enforce()`'s checks in a route.

`AuthzMiddleware` checks every request instead, as (user, path, method) with the user of
Starlette's `AuthenticationMiddleware`, and refuses requests without one. Add it before
`AuthenticationMiddleware`, so that it runs after it. With `anonymous="*anonymous"`, those are
checked as that subject, and the policy opens the public routes (`/docs`, `/login`…) to it:

```python
from starlette.middleware.authentication import AuthenticationMiddleware

from fastapi_alcazaba.authz import AuthzMiddleware

app.add_middleware(AuthzMiddleware, enforcer=enforcer, anonymous="*anonymous")
app.add_middleware(AuthenticationMiddleware, backend=JWTBackend())
```

Refusals get a 403 with `{"detail": "Forbidden"}`, like FastAPI's errors, or a 401 with
`WWW-Authenticate` when there's no user (or `anonymous` is refused), and a 400 for paths with
control characters; WebSockets are closed with 1008. `OPTIONS` requests are checked like the
others: add `CORSMiddleware` after `AuthzMiddleware` (outside it), to answer the preflights.
