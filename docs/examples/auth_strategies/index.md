# Authentication strategies

--8<-- "docs/examples/animations/auth_strategies.svg"

Each strategy has an example of its own, a small app to run and try:

- [`JWTStrategy`](jwt.md): scripts log in with a password and send a JWT, which they can revoke.
- [`OAuth2Strategy`](oauth2.md): people log in with their GitHub account.
- [`SessionStrategy`](session.md): a password, then a session that remembers who logged in.
- [`HTTPBearerStrategy`](http-bearer.md): personal access tokens, refused as soon as they're
  deleted.
- [`SAMLStrategy`](saml.md): single sign-on with a SAML identity provider.
- [Your own strategy](your-own.md): API keys in `X-API-Key`.

This one puts three [strategies](../../guide/authentication/index.md#strategies) together.
People log in with their GitHub account; their scripts, with tokens they create.
`OAuth2Strategy` logs people in with GitHub, `SessionStrategy` remembers them in a server-side
session afterwards, and `JWTStrategy` reads the tokens of their scripts. `/me` takes the last
two: `auth.authenticate("session", "jwt")` tries them in order.

## Run it

Create a GitHub OAuth app (GitHub's Settings → Developer settings → OAuth Apps) whose callback
URL is `http://localhost:8000/auth/github/callback`. Then, from a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
export GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=...
uvicorn examples.auth_strategies.main:app
```

Set `JWT_SECRET` and `CACHE_SIGNING_KEY` too (`secrets.token_urlsafe(32)` each), the same in
every process. Without them each start makes up throwaway ones, and the sessions and tokens stop
working when the app restarts. Open the app at `http://localhost:8000`, not `127.0.0.1`: GitHub
only sends people back to the callback URL it knows.

## Try it

Without a session or a token, nobody:

```console
$ curl -i http://localhost:8000/me
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}
```

Open <http://localhost:8000/login/github> in the browser and log in with GitHub, as in
[the OAuth2 example](oauth2.md): the app starts a session and takes you to `/me`. For a GitHub
user called octocat:

```json
{"id":"github:583231","login":"octocat","name":"The Octocat"}
```

In the same browser, open <http://localhost:8000/docs> and try `POST /tokens`: it makes a token
for your scripts, valid for an hour:

```json
{"access_token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJnaXRodWI6NTgzMjMxIi...","token_type":"bearer","expires_in":3600}
```

A script uses it instead of the session:

```console
$ TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJnaXRodWI6NTgzMjMxIi...
$ curl http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
{"id":"github:583231","login":"octocat","name":"The Octocat"}
```

But it can't make tokens: `/tokens` only takes the session, so a leaked token can't make more.
With only the session strategy tried, the 401 has no challenge:

```console
$ curl -i -X POST http://localhost:8000/tokens -H "Authorization: Bearer $TOKEN"
HTTP/1.1 401 Unauthorized

{"detail":"Not authenticated"}
```

The script can revoke its token, which is refused from then on, until it would have expired:

```console
$ curl -i -X DELETE http://localhost:8000/tokens/current -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content

$ curl -i http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}
```

## What to look at

- **One dependency per way in:** `GitHubUser`, `SessionUser`, `TokenUser` and `AnyUser` say which
  strategies each route accepts. The callback only takes GitHub's answer, `/tokens` only a
  session, `/me` a session or a token.
- **In order:** `AnyUser` tries the session first, then the token. A strategy that finds nothing
  of its own in the request (no cookie, no `Authorization`) passes to the next one; when none
  finds a user, the 401 carries the challenges of those tried.
- **The app owns its users:** `find_or_create_user` turns GitHub's profile into a user of the app,
  created the first time. Sessions and tokens carry the app's user id, never GitHub's token.
- **CSRF:** the session is a cookie, which browsers send by themselves; `CSRFMiddleware` refuses
  what other sites make them send. Requests with a token aren't checked: only the app's pages
  (or a script) can add an `Authorization` header.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/main.py"
--8<-- "examples/auth_strategies/main.py"
```
<!-- fmt: on -->
