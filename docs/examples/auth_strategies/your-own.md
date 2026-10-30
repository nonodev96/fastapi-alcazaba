# Your own strategy

--8<-- "docs/examples/animations/auth_your_own.svg"

A strategy of your own: API keys in `X-API-Key`, for the programs that call the API. It hands
the key to `FastAPIAPIKeys` and gives `FastAPIAuth` the key's owner. Writing one:
[Your own strategy](../../guide/authentication/your-own.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.auth_strategies.api_key_strategy:app
```

Set `API_KEY_PEPPER` and `CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)` each), the same in
every process. Without them each start makes up throwaway ones, and the keys stop working when
the app restarts.

## Try it

Without a key, nobody; the 401 says which credential the app takes:

```console
$ curl -i http://localhost:8000/me
HTTP/1.1 401 Unauthorized
www-authenticate: APIKey

{"detail":"Not authenticated"}
```

Create a key as octocat, whose password is `correct horse battery staple`. It's shown this once:

```console
$ curl -X POST http://localhost:8000/api-keys -H "Content-Type: application/json" -d '{"login": "octocat", "password": "correct horse battery staple"}'
{"id":"pij7glsba4npknkb","key":"demo_pij7glsba4npknkb_u5n6cue325sg5q5z4vlsu6pt275tu2xp6gajqsyxc6cdgu3gl2ca","expires_at":"2027-01-27T15:16:06.323588Z"}
```

A program sends it with each request:

```console
$ KEY=demo_pij7glsba4npknkb_u5n6cue325sg5q5z4vlsu6pt275tu2xp6gajqsyxc6cdgu3gl2ca
$ curl http://localhost:8000/me -H "X-API-Key: $KEY"
{"id":"1","login":"octocat","name":"The Octocat"}
```

A wrong key gets the same 401 as no key, never telling which part was wrong:

```console
$ curl -i http://localhost:8000/me -H "X-API-Key: demo_pij7glsba4npknkb_u5n6cue325sg5q5z4vlsu6pt275tu2xp6gajqsyxc6cdgu3gAAAA"
HTTP/1.1 401 Unauthorized
www-authenticate: APIKey

{"detail":"Not authenticated"}
```

## What to look at

- **Three answers:** `authenticate()` returns the user; None when the request has no
  `X-API-Key`, so that the next strategy is tried; or raises `AuthenticationFailed` when the key
  is wrong, which is logged and never sent. `challenge` is the `WWW-Authenticate` of the 401s.
- **With the others:** as a strategy, the key goes with sessions or JWTs on the same route:
  `auth.authenticate("api-key", "jwt")` takes either.
- **In the docs:** `openapi_scheme()` puts the header in the OpenAPI schema, for the Authorize
  button of `/docs`.
- **Keys:** `FastAPIAPIKeys` keeps their digests, bound to the server's pepper, and looks them up
  by their public id (`pij7glsba4npknkb`); the `demo_` prefix lets secret scanners spot leaked
  ones.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/api_key_strategy.py"
--8<-- "examples/auth_strategies/api_key_strategy.py"
```
<!-- fmt: on -->
