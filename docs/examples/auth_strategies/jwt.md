# JWTStrategy

--8<-- "docs/examples/animations/auth_jwt.svg"

An API for scripts and apps: they log in with a password and get a JWT, which they send in
`Authorization: Bearer` for 15 minutes, and a refresh token, which gets them a new pair when it
expires, until they log out. The strategy: [JWTStrategy](../../guide/authentication/jwt.md) and
its [refresh tokens](../../guide/authentication/jwt.md#refresh-tokens).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.auth_strategies.jwt_tokens:app
```

Set `JWT_SECRET`, `REFRESH_SECRET` and `CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)` each),
the same in every process. Without them each start makes up throwaway ones, and the tokens stop working when the
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
{"access_token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzkzNjYzMTQ2LCJleHAiOjE3OTM2NjQwNDYsImp0aSI6Im4yWkJaZXRTYkd1cllNQmJMQWIxdGciLCJpc3MiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAiLCJhdWQiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAvYXBpIiwic2lkIjoiSGlsQzVKRlRQMHdHQVFrZk15THBJdyJ9.9Jk5ssFHN1ScTCTfQ60XrZhBDWWDYQF7w3HEEL0XOKc","refresh_token":"r1.HilC5JFTP0wGAQkfMyLpIw.0.TCpsBYBjfQlTsg2ZnfHjxFQx9sGDDPJ_-hdW9xR5PV4","token_type":"bearer","expires_in":900,"refresh_expires_in":2591999,"session":"HilC5JFTP0wGAQkfMyLpIw"}
```

The script sends the access token with each request:

```console
$ TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzkzNjYzMTQ2LCJleHAiOjE3OTM2NjQwNDYsImp0aSI6Im4yWkJaZXRTYkd1cllNQmJMQWIxdGciLCJpc3MiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAiLCJhdWQiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAvYXBpIiwic2lkIjoiSGlsQzVKRlRQMHdHQVFrZk15THBJdyJ9.9Jk5ssFHN1ScTCTfQ60XrZhBDWWDYQF7w3HEEL0XOKc
$ curl http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
{"id":"1","login":"octocat","name":"The Octocat"}
```

When it expires, 15 minutes later, the refresh token gets a new pair, and is used up:

```console
$ curl -X POST http://localhost:8000/token/refresh -H "Content-Type: application/json" -d '{"refresh_token": "r1.HilC5JFTP0wGAQkfMyLpIw.0.TCpsBYBjfQlTsg2ZnfHjxFQx9sGDDPJ_-hdW9xR5PV4"}'
{"access_token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzkzNjYzMTQ2LCJleHAiOjE3OTM2NjQwNDYsImp0aSI6ImFUYzBKVHphOGl0dEFXYUxGU3h1blEiLCJpc3MiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAiLCJhdWQiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAvYXBpIiwic2lkIjoiSGlsQzVKRlRQMHdHQVFrZk15THBJdyJ9.9PvmHE-vAiYklyJi6prSek3qCgQ7x3KJqKiK5H8Ycj0","refresh_token":"r1.HilC5JFTP0wGAQkfMyLpIw.1.CcXRawc697P2f-imSUh3QVQ0EQmrbdttvw9VUoxvvj0","token_type":"bearer","expires_in":900,"refresh_expires_in":2591999,"session":"HilC5JFTP0wGAQkfMyLpIw"}
```

A refresh token that comes back after it was exchanged (10 seconds later, or older) was copied:
the login ends, for whoever holds its tokens.

```console
$ curl -i -X POST http://localhost:8000/token/refresh -H "Content-Type: application/json" -d '{"refresh_token": "r1.HilC5JFTP0wGAQkfMyLpIw.0.TCpsBYBjfQlTsg2ZnfHjxFQx9sGDDPJ_-hdW9xR5PV4"}'
HTTP/1.1 401 Unauthorized

{"detail":"Log in again"}

$ curl -i http://localhost:8000/me -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNzkzNjYzMTQ2LCJleHAiOjE3OTM2NjQwNDYsImp0aSI6ImFUYzBKVHphOGl0dEFXYUxGU3h1blEiLCJpc3MiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAiLCJhdWQiOiJodHRwOi8vbG9jYWxob3N0OjgwMDAvYXBpIiwic2lkIjoiSGlsQzVKRlRQMHdHQVFrZk15THBJdyJ9.9PvmHE-vAiYklyJi6prSek3qCgQ7x3KJqKiK5H8Ycj0"
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}
```

Logging out ends the login the same way: its access and refresh tokens are refused from then on.

```console
$ curl -i -X DELETE http://localhost:8000/token -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content
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
  can read: `{"sub":"1","iat":1793663146,"exp":1793664046,"jti":"n2ZBZetSbGurYMBbLAb1tg",
  "iss":"http://localhost:8000","aud":"http://localhost:8000/api","sid":"HilC5JFTP0wGAQkfMyLpIw"}`. The signature only stops
  anyone from changing them: put nothing secret in a JWT.
- **Whose and for whom:** `issuer` and `audience` go into each token and are checked on each
  request, so a token that another app signed with the same secret, or that the same identity
  provider issued for another app, is refused.
- **No lookup:** the user's id is in `sub`, so `verify` loads the user without looking the token
  up. That's also why a token can't be taken back by deleting it: `TokenBlocklist` keeps the
  revoked ones, and the logins (`sid`) that ended, in the cache's backend, until they expire.
- **Short tokens, long logins:** access tokens last 15 minutes; `RefreshTokens` keeps each login
  for 30 days (7 without a refresh), rotating its refresh token at each exchange. The backend
  keeps a record per login, never a refresh token: whoever reads it can't make one.
- **Passwords:** kept as scrypt digests, compared in constant time, and unknown logins take as
  long as wrong passwords. `FastAPIRateLimiter` limits the tries per address.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/jwt_tokens.py"
--8<-- "examples/auth_strategies/jwt_tokens.py"
```
<!-- fmt: on -->
