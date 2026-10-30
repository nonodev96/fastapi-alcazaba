# HTTPBearerStrategy

--8<-- "docs/examples/animations/auth_bearer.svg"

Personal access tokens: random strings that users create for their scripts, kept as digests,
and refused as soon as they're deleted. The strategy:
[HTTPBearerStrategy](../../guide/authentication/http-bearer.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.auth_strategies.bearer_tokens:app
```

## Try it

Create a token as octocat, whose password is `correct horse battery staple`. It's shown this
once: the database only keeps its digest.

```console
$ curl -X POST http://localhost:8000/tokens -H "Content-Type: application/json" -d '{"login": "octocat", "password": "correct horse battery staple"}'
{"token":"pat_UrPE4G3pn56_cquuCHw2c52IcAn6X-4aYzCFvmvWohA","created_at":"2026-10-29T15:16:05.570276Z"}
```

A script sends it with each request:

```console
$ TOKEN=pat_UrPE4G3pn56_cquuCHw2c52IcAn6X-4aYzCFvmvWohA
$ curl http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
{"id":"1","login":"octocat","name":"The Octocat"}
```

Delete it, and the next request with it is refused:

```console
$ curl -i -X DELETE http://localhost:8000/tokens/current -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content

$ curl -i http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}
```

## What to look at

- **Digests, not tokens:** the database keeps the SHA-256 of each token. Whoever reads it (a
  backup, a leak) gets nothing that works. The tokens are random and long, so a plain hash is
  enough: there's no password to guess.
- **Deleted, refused:** unlike a JWT, the token carries nothing, so the app looks it up on each
  request. That costs a query, and buys taking a token back at once, in every process.
- **Before `find_token`:** a token that isn't RFC 6750's syntax, or longer than 4 KiB, is refused
  without reaching your function.
- **A prefix:** `pat_` lets secret scanners (GitHub's, GitLab's) spot tokens leaked in code.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/bearer_tokens.py"
--8<-- "examples/auth_strategies/bearer_tokens.py"
```
<!-- fmt: on -->
