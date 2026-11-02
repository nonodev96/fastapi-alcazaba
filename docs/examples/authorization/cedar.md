# Cedar

--8<-- "docs/examples/animations/authz_cedar.svg"

A team's shared documents, whose [Cedar](../../guide/authorization/cedar.md) policies decide who
reads, edits and deletes each one by its attributes: its team, its owner, whether it's
confidential, and who's asking. The members of a document's team read it, its owner edits it,
admins delete documents, and contractors never see a confidential one, whatever else allows it.

Users log in with a fixed token each, which stands in for your authentication: `ana-token`
(admin, editorial), `ben-token` (editorial), `eva-token` (a contractor in editorial) and
`dan-token` (sales).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.authz.cedar_policies:app
```

## Try it

Ben reads both documents of his team, and none of sales. Eva, in the same team, doesn't get the
confidential budget:

```console
$ curl http://127.0.0.1:8000/documents -H "Authorization: Bearer ben-token"
[{"id":1,"title":"Style guide","owner":"ana","team":"editorial","confidential":false},{"id":2,"title":"Budget 2027","owner":"ben","team":"editorial","confidential":true}]

$ curl http://127.0.0.1:8000/documents -H "Authorization: Bearer eva-token"
[{"id":1,"title":"Style guide","owner":"ana","team":"editorial","confidential":false}]

$ curl -i http://127.0.0.1:8000/documents/2 -H "Authorization: Bearer eva-token"
HTTP/1.1 404 Not Found
content-type: application/json

{"detail":"No such document"}
```

Only its owner edits a document, admins included:

```console
$ curl -i -X PUT http://127.0.0.1:8000/documents/2 -H "Authorization: Bearer ana-token" -H "Content-Type: application/json" -d '{"title": "Budget 2028"}'
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"Only its owner edits a document"}

$ curl -X PUT http://127.0.0.1:8000/documents/2 -H "Authorization: Bearer ben-token" -H "Content-Type: application/json" -d '{"title": "Budget 2027, second draft"}'
{"id":2,"title":"Budget 2027, second draft","owner":"ben","team":"editorial","confidential":true}
```

Deleting is for admins, asked as a named action, `(user, documents, delete)`:

```console
$ curl -i -X DELETE http://127.0.0.1:8000/documents/3 -H "Authorization: Bearer dan-token"
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"Forbidden"}

$ curl -i -X DELETE http://127.0.0.1:8000/documents/3 -H "Authorization: Bearer ana-token"
HTTP/1.1 204 No Content
content-type: application/json
```

Without a token, a 401 before the policy is asked:

```console
$ curl -i http://127.0.0.1:8000/documents
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer
content-type: application/json

{"detail":"Not authenticated"}
```

## What to look at

- **Attributes, not roles alone:** the user's team and whether it's a contractor, the document's
  team, owner and confidentiality. The user is the entity the authentication gives; each
  document, the entity the route builds from the row it read.
- **A forbid beats every permit:** Eva is in the budget's team, which permits reading it, and
  the forbid on confidential documents for contractors wins.
- **Validated at start:** the policies are checked against `schema.cedarschema` when the enforcer
  is made, so a misspelled attribute fails at start, not as a wrong answer.
- **404 for what you may not read:** a document a user may not read doesn't exist for them.

## The code

```cedar title="examples/authz/policies.cedar"
--8<-- "examples/authz/policies.cedar"
```

```text title="examples/authz/schema.cedarschema"
--8<-- "examples/authz/schema.cedarschema"
```

<!-- fmt: off -->
```python title="examples/authz/cedar_policies.py"
--8<-- "examples/authz/cedar_policies.py"
```
<!-- fmt: on -->
