# OAuth2Strategy

--8<-- "docs/examples/animations/auth_oauth2.svg"

People log in with their GitHub account: the authorization code flow with PKCE, then a session
that remembers them. The strategy: [OAuth2Strategy](../../guide/authentication/oauth2.md).

## Run it

Create a GitHub OAuth app (GitHub's Settings → Developer settings → OAuth Apps) whose callback
URL is `http://localhost:8000/auth/github/callback`. Then, from a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
export GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=...
uvicorn examples.auth_strategies.oauth2_login:app
```

Set `CACHE_SIGNING_KEY` too (`secrets.token_urlsafe(32)`), the same in every process. Open the
app at `http://localhost:8000`, not `127.0.0.1`: GitHub only sends people back to the callback
URL it knows.

## Try it

`/login/github` sends the browser to GitHub, with a random state and a PKCE challenge. The state
and the PKCE verifier wait in a cookie, for the callback to check:

```console
$ curl -i http://localhost:8000/login/github
HTTP/1.1 302 Found
location: https://github.com/login/oauth/authorize?response_type=code&client_id=...&redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fauth%2Fgithub%2Fcallback&state=jXJcV-_O0q0_v9K2MfyEfeMjEah55SvgW9sSoO3rjmY&code_challenge=c19OME8_wXKNeiwWUU70d6uUfTS2mdbbKUgEioMybbI&code_challenge_method=S256&scope=read%3Auser
set-cookie: __Host-alcazaba-oauth2-github=jXJcV-_O0q0_v9K2MfyEfeMjEah55SvgW9sSoO3rjmY.iDVwXX46...; Max-Age=600; Path=/; HttpOnly; Secure; SameSite=Lax
cache-control: no-store
```

Open <http://localhost:8000/login/github> in the browser. GitHub asks whether to let the app read
your profile, then sends you back to the callback: the strategy checks the state, exchanges the
code for a token and reads your profile, and the app starts a session and takes you to `/me`.
For a GitHub user called octocat:

```json
{"id":"github:583231","login":"octocat","name":"The Octocat"}
```

## What to look at

- **Only on the callback:** `GitHubUser` is the only dependency with the `github` strategy, and
  only the callback takes it. Elsewhere the strategy finds nobody, and the session tells who's
  there.
- **A login nobody can force on you:** the callback is refused without the cookie that
  `/login/github` set, or with another state, so a link to the callback with someone else's code
  doesn't log you in as them.
- **The app owns its users:** `find_or_create_user` turns GitHub's profile into a user of the
  app, created the first time. The session carries the app's user id, never GitHub's token.
- **CSRF:** the session is a cookie, which browsers send by themselves; `CSRFMiddleware` refuses
  what other sites make them send, such as a form that posts to `/logout`.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/oauth2_login.py"
--8<-- "examples/auth_strategies/oauth2_login.py"
```
<!-- fmt: on -->
