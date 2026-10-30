# Authorization with Casbin

--8<-- "docs/examples/animations/authz.svg"

A newsroom API whose policy, not its routes, says who may do what. Anybody reads the published
articles; writers write drafts, editors edit and publish them, and admins give people roles. The
[`FastAPICasbin`](../guide/authz.md#casbin-policies) dependencies check each request against
Casbin's policy: on the path and method of the route, or on a named action such as
`(ben, articles, publish)`.

Users log in with a fixed token each, which stands in for your authentication (JWTs, sessions,
[API keys](auth.md)): `ana-token` (admin), `ben-token` (editor), `cris-token` (writer) and
`dan-token` (no role yet).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.authz.main:app
```

## Try it

Visitors read the published articles. Cris, a writer, reads the drafts too:

```console
$ curl http://127.0.0.1:8000/articles
[{"title":"Olive harvest starts","body":"...","id":1,"author":"cris","published_by":"ben"}]

$ curl http://127.0.0.1:8000/articles -H "Authorization: Bearer cris-token"
[{"title":"Olive harvest starts",...,"published_by":"ben"},{"title":"Draft: new mill","body":"...","id":2,"author":"cris","published_by":null}]
```

Writing needs an account, and the writer role:

```console
$ curl -i -X POST http://127.0.0.1:8000/articles -H "Content-Type: application/json" \
    -d '{"title": "Mill opens", "body": "On Monday."}'
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}

$ curl -X POST http://127.0.0.1:8000/articles -H "Authorization: Bearer cris-token" \
    -H "Content-Type: application/json" -d '{"title": "Mill opens", "body": "On Monday."}'
{"title":"Mill opens","body":"On Monday.","id":3,"author":"cris","published_by":null}
```

Publishing is a named action, `(user, articles, publish)`, which only editors may do:

```console
$ curl -i -X POST http://127.0.0.1:8000/articles/3/publish -H "Authorization: Bearer cris-token"
HTTP/1.1 403 Forbidden

{"detail":"Forbidden"}

$ curl -X POST http://127.0.0.1:8000/articles/3/publish -H "Authorization: Bearer ben-token"
{"title":"Mill opens","body":"On Monday.","id":3,"author":"cris","published_by":"ben"}
```

Dan has an account but no role, and so reads what visitors read, and nothing more. Ana, an
admin, makes Dan a writer, which takes effect at once:

```console
$ curl -i -X POST http://127.0.0.1:8000/articles -H "Authorization: Bearer dan-token" \
    -H "Content-Type: application/json" -d '{"title": "Hello", "body": "First post."}'
HTTP/1.1 403 Forbidden

{"detail":"Forbidden"}

$ curl -i -X PUT http://127.0.0.1:8000/users/dan/roles/writer -H "Authorization: Bearer ana-token"
HTTP/1.1 204 No Content

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/articles \
    -H "Authorization: Bearer dan-token" -H "Content-Type: application/json" \
    -d '{"title": "Hello", "body": "First post."}'
201
```

Admins only grant the roles the policy knows:

```console
$ curl -X PUT http://127.0.0.1:8000/users/dan/roles/root -H "Authorization: Bearer ana-token"
{"detail":[{"type":"enum","loc":["path","role"],"msg":"Input should be 'writer', 'editor' or 'admin'",...}]}
```

A wrong token is refused, rather than taken for a visitor without one:

```console
$ curl -i http://127.0.0.1:8000/articles -H "Authorization: Bearer nonsense"
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Invalid token"}
```

## What to look at

- **The policy is data:** `policy.csv` has a `p` line per permission of a role and a `g` line
  per role of a user (or of another role: editors are writers too). Changing who may do what
  doesn't change the code; in production the policy comes from a database, through one of
  Casbin's async adapters.
- **Two ways to ask:** `Depends(authz)` asks about the route, as (user, path, method): Casbin's
  `keyMatch2` takes `/articles/:id` as a pattern. `members.require("articles", "publish")` asks
  about a named action, the same on whatever route it guards.
- **Visitors and members:** `authz` has the policy check visitors without a token as
  `anonymous`, so that the policy opens the reading routes to them. `members` refuses them with
  a 401 before asking the policy, and so always returns a user name: the routes for members
  don't deal with None.
- **What anonymous may do, everybody may:** the matcher grants the rules of `anonymous` to every
  subject (`p.sub == "anonymous" || g(r.sub, p.sub)`), so Dan, without a role, isn't worse off
  than a visitor.
- **Checks that depend on the data** go in the route, with `authz.enforce()`: everybody may list
  the articles, but only those who may read the drafts see them. A draft is a 404 for the others,
  not a 403, which would tell that it exists.
- **Roles granted at runtime** take effect at once in the process that grants them: an
  `AsyncEnforcer` changes its policy in the event loop, where the checks run. The file adapter of
  the example doesn't keep them; a database adapter does, and a Casbin watcher tells the other
  processes to load the policy again.

## The code

=== "main.py"

    <!-- fmt: off -->
    ```python title="examples/authz/main.py"
    --8<-- "examples/authz/main.py"
    ```
    <!-- fmt: on -->

=== "model.conf"

    ```ini title="examples/authz/model.conf"
    --8<-- "examples/authz/model.conf"
    ```

=== "policy.csv"

    ```text title="examples/authz/policy.csv"
    --8<-- "examples/authz/policy.csv"
    ```
