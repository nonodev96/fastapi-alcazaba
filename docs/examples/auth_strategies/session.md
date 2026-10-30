# SessionStrategy

--8<-- "docs/examples/animations/auth_session.svg"

A web app's login: a password, then a server-side session that remembers who logged in. The
browser only holds a random id, in a cookie. The strategy:
[SessionStrategy](../../guide/authentication/session.md).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.auth_strategies.session_login:app
```

Set `CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)`), the same in every process. Without it
each start makes up a throwaway one, and the sessions stop working when the app restarts.

## Try it

Log in as octocat, whose password is `correct horse battery staple`. curl keeps the session's
cookie in `cookies.txt`, as a browser would:

```console
$ curl -i -c cookies.txt -X POST http://localhost:8000/login -H "Content-Type: application/json" -d '{"login": "octocat", "password": "correct horse battery staple"}'
HTTP/1.1 200 OK
set-cookie: session=NZ4ZNmaZUVX7u7ZU4LB4AA4T8QDdE91BoXMzbXLKmJc; Max-Age=1209600; Path=/; HttpOnly; Secure; SameSite=Lax
cache-control: no-store

{"id":"1","login":"octocat","name":"The Octocat"}

$ curl -b cookies.txt http://localhost:8000/me
{"id":"1","login":"octocat","name":"The Octocat"}
```

Logging out is refused without the app's `Origin`: `CSRFMiddleware` can't tell that the request
comes from the app's own pages, as browsers say it with their `Origin` and `Sec-Fetch-Site`
headers.

```console
$ curl -i -b cookies.txt -X POST http://localhost:8000/logout
HTTP/1.1 403 Forbidden

{"detail":"Cross-site request refused"}

$ curl -i -b cookies.txt -X POST http://localhost:8000/logout -H "Origin: http://localhost:8000"
HTTP/1.1 204 No Content
set-cookie: session=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/; HttpOnly; Secure; SameSite=Lax
cache-control: no-store
```

The session is deleted from the backend, so the cookie that `cookies.txt` still holds is refused:

```console
$ curl -i -b cookies.txt http://localhost:8000/me
HTTP/1.1 401 Unauthorized

{"detail":"Not authenticated"}
```

## What to look at

- **The user id, not the user:** the session keeps the app's user id, and `load_user` loads the
  user on each request. A session whose user doesn't exist any more fails.
- **A new session at login:** `sessions.login()` gives the session a new id, so one planted in the
  browser before (session fixation) doesn't get logged in too.
- **The cookie:** HttpOnly (scripts in the page can't read it), Secure and SameSite=Lax, with
  `Cache-Control: no-store` on the responses that set it.
- **Passwords:** kept as scrypt digests, compared in constant time, and unknown logins take as
  long as wrong passwords. `FastAPIRateLimiter` limits the tries per address.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/session_login.py"
--8<-- "examples/auth_strategies/session_login.py"
```
<!-- fmt: on -->
