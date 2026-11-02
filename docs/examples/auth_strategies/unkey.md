# UnkeyStrategy

--8<-- "docs/examples/animations/auth_unkey.svg"

API keys that Unkey keeps: people sign up and get a key, their scripts send it, and Unkey checks
it on every request, with its permissions, its credits and its rate limit. The strategy:
[UnkeyStrategy](../../guide/authentication/unkey.md).

Each key reads documents (`documents.read`), has 100 credits a month and 10 requests a minute.
Writing a document takes 5 credits and needs `documents.write`, which signing up doesn't give.

## Run it

Create an API in [Unkey's dashboard](https://app.unkey.com), and two root keys: one that may
only verify its keys, one that may create and delete them. Then, from a clone of the
repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
export UNKEY_VERIFY_KEY=... UNKEY_ADMIN_KEY=... UNKEY_API_ID=api_...
uvicorn examples.auth_strategies.unkey_keys:app
```

## Try it

Sign up: the key is shown this once, Unkey keeps only its hash.

```console
$ curl -X POST http://localhost:8000/signup -H "Content-Type: application/json" -d '{"username": "ana"}'
{"key":"docs_...","key_id":"key_..."}
```

The key reads documents; each request takes a credit and one of the minute's 10:

```console
$ curl http://localhost:8000/documents -H "Authorization: Bearer docs_..."
[{"title":"Style guide","owner":"unkey"}]
```

Writing needs `documents.write`, which Ana's key doesn't have: a 403. Give it the permission in
Unkey's dashboard, and the same key writes, for 5 credits:

```console
$ curl -i -X POST http://localhost:8000/documents -H "Authorization: Bearer docs_..." -H "Content-Type: application/json" -d '{"title": "Draft"}'
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"Insufficient permissions"}
```

The eleventh request of a minute gets a 429 with `Retry-After`, and a key out of credits a 429
too (`{"detail":"No credits left"}`) until they're refilled on the 1st. Ana deletes her key, and
Unkey refuses it from then on (within 30 seconds):

```console
$ curl -i -X DELETE http://localhost:8000/keys/key_... -H "Authorization: Bearer docs_..."
HTTP/1.1 204 No Content

$ curl -i http://localhost:8000/documents -H "Authorization: Bearer docs_..."
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer
content-type: application/json

{"detail":"Not authenticated"}
```

## What to look at

- **Two root keys:** the strategy's may only verify keys, `UnkeyKeys`' may create and delete
  them: a leak of the first can't make keys.
- **The user from the key:** each key is made with `external_id=user.id`, and `load_user` gets
  it back from every verification.
- **A strategy per permission:** writing has a strategy of its own, `unkey-write`, with its
  permission and its cost, and the same client.
- **Only the owner deletes a key:** a key ID that isn't the user's is a 404.
- **Unkey down:** the requests get a 503, never let through.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/unkey_keys.py"
--8<-- "examples/auth_strategies/unkey_keys.py"
```
<!-- fmt: on -->
