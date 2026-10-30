# JWTStrategy

--8<-- "docs/examples/animations/auth_jwt.svg"

An API for scripts: they log in with a password and get a JWT, which they send in
`Authorization: Bearer` until it expires, or until they revoke it. The strategy:
[JWTStrategy](../../guide/authentication/jwt.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.auth_strategies.jwt_tokens:app
```

Set `JWT_SECRET` and `CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)` each), the same in every
process. Without them each start makes up throwaway ones, and the tokens stop working when the
app restarts.

## Try it

Without a token, nobody:

```console
$ curl -i http://localhost:8000/me
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}
```

Log in as octocat, whose password is `correct horse battery staple`:

```console
$ curl -X POST http://localhost:8000/token -H "Content-Type: application/json" -d '{"login": "octocat", "password": "correct horse battery staple"}'
{"access_token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzkzMjg2OTUzLCJleHAiOjE3OTMyOTA1NTMsImp0aSI6InJXYlhvNTJ1XzJubEEzOTdmWlk5UkEiLCJpc3MiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAiLCJhdWQiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAvYXBpIn0.rK7JObLs_Ret8PpgyoIB5OfMw0YNJuZun9f_Y-qEo8M","token_type":"bearer","expires_in":3600}
```

The script sends the token with each request:

```console
$ TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzkzMjg2OTUzLCJleHAiOjE3OTMyOTA1NTMsImp0aSI6InJXYlhvNTJ1XzJubEEzOTdmWlk5UkEiLCJpc3MiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAiLCJhdWQiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAvYXBpIn0.rK7JObLs_Ret8PpgyoIB5OfMw0YNJuZun9f_Y-qEo8M
$ curl http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
{"id":"1","login":"octocat","name":"The Octocat"}
```

And revokes it when it's done: the token is refused from then on, until it would have expired.

```console
$ curl -i -X DELETE http://localhost:8000/token -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content

$ curl -i http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}
```

Five tries a minute per address: the next one gets a 429, with the right password or not.
After a few wrong ones:

```console
$ curl -i -X POST http://localhost:8000/token -H "Content-Type: application/json" -d '{"login": "octocat", "password": "guess"}'
HTTP/1.1 429 Too Many Requests
ratelimit: "5/60s";r=0;t=19
retry-after: 19

{"detail":"Too Many Requests"}
```

## What to look at

- **Readable, not secret:** the middle part of the token is its claims in base64, which anyone
  can read: `{"sub":"1","iat":1793286953,"exp":1793290553,"jti":"rWbXo52u_2nlA397fZY9RA",
  "iss":"http://localhost:8000","aud":"http://localhost:8000/api"}`. The signature only stops
  anyone from changing them: put nothing secret in a JWT.
- **Whose and for whom:** `issuer` and `audience` go into each token and are checked on each
  request, so a token that another app signed with the same secret, or that the same identity
  provider issued for another app, is refused.
- **No lookup:** the user's id is in `sub`, so `verify` loads the user without looking the token
  up. That's also why a token can't be taken back by deleting it: `TokenBlocklist` keeps the
  revoked ones, in the cache's backend, until they expire.
- **Passwords:** kept as scrypt digests, compared in constant time, and unknown logins take as
  long as wrong passwords. `FastAPIRateLimiter` limits the tries per address.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/jwt_tokens.py"
--8<-- "examples/auth_strategies/jwt_tokens.py"
```
<!-- fmt: on -->
