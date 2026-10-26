# A browser app: sessions, CSRF and security headers

A small shop rendered on the server, with plain HTML and a few lines of JavaScript. Who signed in
and the cart live in a server-side session
([`ServerSessionMiddleware`](../index.md#server-side-sessions)): the cookie only carries a random
id, which signing in replaces and signing out deletes, and 30 minutes without requests end it.
[`CSRFMiddleware`](../index.md#csrf-protection) refuses what pages on other sites make the
browser send, and with `require_token=True` also wants the page's token in a header.
[`SecurityHeadersMiddleware` and `BodySizeLimitMiddleware`](../index.md#security-headers-and-body-size)
add the security headers, with a Content-Security-Policy for the pages, and refuse large bodies. A
[`FastAPIRateLimiter`](../index.md#rate-limiting) slows down password guessing.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.browser_app.main:app
```

Open <http://localhost:8000> and sign in as `ana`, password `correct horse battery staple`.

The cookies are `Secure`: browsers only keep them from HTTPS sites and from `http://localhost`,
which they treat as secure. Use `localhost`, not a LAN address, to try it over plain HTTP. In
tests, give the client an https URL for the same reason:
`TestClient(app, base_url="https://testserver")`.

Set `SESSION_SIGNING_KEY` (`secrets.token_urlsafe(32)`), the same in every process. Without it
each start makes up a throwaway one, and every session ends when the app restarts.

## Try it

curl keeps `Secure` cookies from `localhost` too, so a cookie jar plays the browser. The first
page sets the CSRF token, in a cookie and in the page, and carries the security headers:

```console
$ curl -i -c jar -b jar http://localhost:8000/
HTTP/1.1 200 OK
content-type: text/html; charset=utf-8
vary: Cookie
set-cookie: __Host-csrftoken=QkgoLDXsFG6SGZsxRY39XJJJWKwuVB1VQCAoC7HQIpc; Path=/; Secure; SameSite=Lax
x-content-type-options: nosniff
x-frame-options: DENY
referrer-policy: no-referrer
cross-origin-opener-policy: same-origin
content-security-policy: default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'

<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Shop</title><meta name="csrf-token" content="QkgoLDXsFG6SGZsxRY39XJJJWKwuVB1VQCAoC7HQIpc"><script src="/app.js" defer></script></head><body><p><a href="/login">Sign in</a></p>...
```

No session yet: it's only created when there's something to keep. A form posted without the
token is refused, even from the app's own page (`Sec-Fetch-Site: same-origin`, as browsers send):

```console
$ curl -i -c jar -b jar -X POST http://localhost:8000/cart -H "Sec-Fetch-Site: same-origin" \
    -d product=Figs
HTTP/1.1 403 Forbidden

{"detail":"Missing or invalid CSRF token"}
```

With the token in `X-CSRF-Token`, as `app.js` sends it, the cart gets a session:

```console
$ curl -i -c jar -b jar -X POST http://localhost:8000/cart -H "Sec-Fetch-Site: same-origin" \
    -H "X-CSRF-Token: QkgoLDXsFG6SGZsxRY39XJJJWKwuVB1VQCAoC7HQIpc" -d product=Figs
HTTP/1.1 303 See Other
location: /
set-cookie: __Host-session=ozlYn1TlYGLpFmUZlKT0eArPzN2hQZkbFLVVbPWV1II; Max-Age=28800; Path=/; HttpOnly; Secure; SameSite=Lax
cache-control: no-store
```

Signing in gives the session a new id, with the same cart:

```console
$ curl -i -c jar -b jar -X POST http://localhost:8000/login -H "Sec-Fetch-Site: same-origin" \
    -H "X-CSRF-Token: QkgoLDXsFG6SGZsxRY39XJJJWKwuVB1VQCAoC7HQIpc" \
    -d username=ana --data-urlencode "password=correct horse battery staple"
HTTP/1.1 303 See Other
location: /
set-cookie: __Host-session=s4wi5n6b_DRAvO0aFKJ7gzK6lyPtWymwDfZ8Xqm8AJ0; Max-Age=28800; Path=/; HttpOnly; Secure; SameSite=Lax

$ curl -b jar http://localhost:8000/
...<p>Signed in as ana.</p>...<h2>Cart</h2><ul><li>Figs: 1</li></ul></body></html>
```

The old id is worth nothing now, so one planted in the browser before signing in (session
fixation) gives nothing away:

```console
$ curl http://localhost:8000/ -H "Cookie: __Host-session=ozlYn1TlYGLpFmUZlKT0eArPzN2hQZkbFLVVbPWV1II"
...<p><a href="/login">Sign in</a></p>...
```

A page on another site can't sign the user out, even if it had the token:

```console
$ curl -i -b jar -X POST http://localhost:8000/logout -H "Sec-Fetch-Site: cross-site" \
    -H "Origin: https://evil.example" -H "X-CSRF-Token: QkgoLDXsFG6SGZsxRY39XJJJWKwuVB1VQCAoC7HQIpc"
HTTP/1.1 403 Forbidden
x-frame-options: DENY
content-security-policy: default-src 'none'; frame-ancestors 'none'

{"detail":"Cross-site request refused"}
```

Signing out deletes the session from the backend: a copy of the cookie doesn't work either:

```console
$ curl -i -c jar -b jar -X POST http://localhost:8000/logout -H "Sec-Fetch-Site: same-origin" \
    -H "X-CSRF-Token: QkgoLDXsFG6SGZsxRY39XJJJWKwuVB1VQCAoC7HQIpc"
HTTP/1.1 303 See Other
location: /
set-cookie: __Host-session=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/; HttpOnly; Secure; SameSite=Lax

$ curl http://localhost:8000/ -H "Cookie: __Host-session=s4wi5n6b_DRAvO0aFKJ7gzK6lyPtWymwDfZ8Xqm8AJ0"
...<p><a href="/login">Sign in</a></p>...
```

Bodies over 64 KiB are refused before the app reads them:

```console
$ head -c 100000 /dev/zero | tr '\0' x > big.txt
$ curl -i -b jar -X POST http://localhost:8000/cart --data-binary @big.txt
HTTP/1.1 413 Content Too Large
x-content-type-options: nosniff

{"detail":"Request body too large"}
```

## What to look at

- **The order of the middlewares:** each one added wraps those before it. The session is the
  innermost; CSRF goes outside it, so a refused request never reads a session from the backend;
  the security headers are the outermost, so the 403s and 413s of the others get them too.
- **`require_token=True` needs JavaScript:** the middleware reads the token from the
  `X-CSRF-Token` header, which a plain HTML form can't set, so `app.js` sends the forms with
  `fetch`, taking the token from the page's `<meta>` (`request.state.csrf_token`). An app whose
  forms must work without JavaScript leaves `require_token` off: the origin checks alone already
  stop CSRF in every current browser, login CSRF included.
- **A strict Content-Security-Policy for the pages:** only the app's own script, no inline code
  (so the token goes in a `<meta>` and the script in its own file), no framing, forms only to the
  app. The JSON responses keep the default `default-src 'none'`. `/docs` is off: a site has no
  use for it, and its CDN scripts wouldn't pass the policy.
- **`rotate_session` at sign-in, `clear_session` at sign-out:** the first defeats session
  fixation, the second makes a stolen cookie worthless at once. The `__Host-` prefix keeps a
  sibling subdomain from planting either cookie.
- **Small, escaped sessions:** the cart only holds products of an enum, at most 10 of each, so a
  session can't outgrow `max_size`; everything that reaches the HTML goes through `html.escape`.
- **Passwords:** a salted scrypt hash, computed in a thread (it's slow on purpose) and for unknown
  users too, so the time doesn't tell who has an account; five attempts a minute per address.

## The code

<!-- fmt: off -->
```python title="examples/browser_app/main.py"
--8<-- "examples/browser_app/main.py"
```
<!-- fmt: on -->
