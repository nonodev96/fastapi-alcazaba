# Sessions, CSRF and security headers

What an app that browsers use needs: sessions it can revoke, requests from other sites refused, and
the headers and limits every response should carry.

In an app: [the example](../examples/browser_app.md). Every class and function: [the API reference](../reference/browser.md).

## Server-side sessions

Starlette's `SessionMiddleware` keeps the whole session in a signed cookie: the client can read
it (it's only base64), it can't be revoked (a copied cookie keeps working after logout, until it
expires) and it can't hold much more than 4 KB. `ServerSessionMiddleware` keeps the data in the
cache's backend instead, and the cookie only carries a random id; `request.session` works the
same:

```python
from fastapi_alcazaba.security import ServerSessionMiddleware, clear_session, rotate_session

app.add_middleware(ServerSessionMiddleware, max_age=8 * 3600, idle_timeout=1800)


@app.post("/login")
async def login(request: Request, form: Annotated[Login, Form()]) -> RedirectResponse:
    user = await authenticate(form)
    request.session["user_id"] = user.id
    rotate_session(request)  # a new id: one planted in the browser before is now worthless
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    clear_session(request)  # deleted from the backend: a copied cookie is worth nothing now
    return RedirectResponse("/", status_code=303)
```

- **The cookie** holds 256 random bits (`secrets.token_urlsafe(32)`), `HttpOnly`, `Secure` and
  `SameSite=Lax` by default, with `Max-Age` = `max_age`, named `__Host-session` so that a sibling
  subdomain can't plant one (cookie tossing). Plain HTTP in development needs `secure=False` and
  another `cookie_name`.
- **The backend** keeps the data under a SHA-256 digest of the id (whoever reads the keys can't use
  them as cookies), sealed with the cache's `signing_key` or `encryption_key` (use the latter when
  sessions hold personal data). Without either, whoever can write to the backend can forge a
  session, and a warning is logged. With a `TieredBackend` only the shared tier is used, so a
  logout is seen by every process at once.
- **Saved only when it changed**, nested changes included (`request.session["cart"].append(...)`),
  when the response starts. Empty sessions aren't stored and get no cookie; a session the app
  empties is deleted and its cookie expired. Responses that read it get `Vary: Cookie`, and those
  that set the cookie `Cache-Control: no-store` unless they set their own.
- **New ids are always the server's:** a cookie whose session doesn't exist (expired, made up)
  never becomes the id of a new session. `rotate_session` gives a new id with the same data (and
  a fresh `max_age`) at login, against session fixation; `clear_session` deletes the session and
  expires the cookie at logout, and data stored afterwards goes to a new session.
- **Expiry:** `max_age` seconds after creation or rotation, and `idle_timeout` seconds without
  requests (refreshed in the backend at most every tenth of it, not on every request). Both are
  checked when the session is read, and the backend drops it then as well.
- **JSON only**, up to `max_size` bytes (16 KiB): more, or a value that isn't JSON, raises when
  the response starts.
- **Backend failures:** if the session can't be read, the request goes on with an empty one
  (anonymous, which fails closed) and a warning. Changing it then, or failing to save, rotate or
  clear it, replaces the response with a 503 and leaves the cookie alone: the response would claim
  a login or a logout that didn't happen. Concurrent requests of one session: the last save wins.
- WebSockets get `websocket.session` too, read-only (`rotate_session` and `clear_session` raise
  there). A plain ASGI middleware: streaming responses still stream.

## CSRF protection

A page on another site can make the user's browser send a form, or a `fetch`, to your app, and
the browser adds the user's cookies by itself. `CSRFMiddleware` refuses those requests with a 403,
from the headers browsers send to say where a request comes from:

```python
from fastapi_alcazaba.security import CSRFMiddleware

app.add_middleware(
    CSRFMiddleware,
    trusted_origins=["https://admin.example.com"],  # other origins whose pages may post here
    exempt_paths=["/webhooks"],  # signed in their own way
)
```

- **Fetch metadata first:** every current browser sends `Sec-Fetch-Site`. `same-origin` (and
  `none`, a request the user typed) goes through; `same-site` and `cross-site` are refused unless
  their `Origin` is in `trusted_origins`. Sibling subdomains are other origins too.
- **Older browsers:** without `Sec-Fetch-Site`, the `Origin` header (or else the `Referer`) must
  be the app's own origin or a trusted one. `Origin: null` (sandboxed frames, redirects) is
  refused, and so is a request with neither header.
- **Only what the browser adds by itself is protected:** requests without cookies or remembered
  HTTP authentication (Basic, Digest, Negotiate, NTLM) go through, so APIs that take a bearer
  token in a header keep working from anywhere (a page on another site can't set the header).
  Except form submissions from other sites (`Sec-Fetch-Mode` other than `cors`): before the user
  has a cookie they would log the browser into the attacker's account (login CSRF).
  `check_all=True` checks every request, for apps that trust TLS client certificates or the
  network.
- **Safe methods** (GET, HEAD, OPTIONS, TRACE) always go through: they mustn't change anything.
- **The app's own origin** is the scheme and the `Host` header as the server sees them. Behind a
  proxy that terminates TLS, let the server trust it (`uvicorn --proxy-headers
  --forwarded-allow-ips=<proxy address>`) and have the proxy pass `Host` on; otherwise every
  request looks cross-site, so list the public origin in `trusted_origins`.
- **Double-submit token** for single-page apps, as defence in depth: with `require_token=True`
  every response to a request without a valid token sets a random one (256 bits) in the
  `__Host-csrftoken` cookie (readable by JavaScript, `Secure`, `SameSite=Lax`, the `__Host-`
  prefix keeps sibling subdomains from planting one), also in `request.state.csrf_token`. Requests
  with cookies must send it back in `X-CSRF-Token`; it's compared in constant time. A browser that
  sends neither Origin nor Referer passes with a valid token. HTML forms can't set headers, so
  with `require_token=True` every form that changes something must be sent by JavaScript (`fetch`
  with the header), the login form included; apps with plain HTML forms leave it off and rely on
  the origin checks, which protect them on their own.
- **WebSockets:** browsers don't apply CORS to WebSocket handshakes, so any site could open one
  with the user's cookies and talk to it (cross-site WebSocket hijacking). Handshakes with cookies
  get the same checks and are closed with 1008 before they're accepted (the server answers 403);
  handshakes without `Origin` aren't from a browser and go through.
- Refusals are logged at INFO with the method and the reason (never cookies or tokens), and
  counted in `fastapi_alcazaba.csrf.refused`. A plain ASGI middleware: streaming responses still
  stream.

## Security headers and body size

Two ASGI middlewares for what FastAPI leaves to the deployment:

```python
from fastapi_alcazaba.security import BodySizeLimitMiddleware, SecurityHeadersMiddleware

app.add_middleware(
    BodySizeLimitMiddleware,
    max_size=1_048_576,  # 1 MiB
    paths={"/uploads": 50 * 1_048_576, "/internal/stream": None},
)
app.add_middleware(SecurityHeadersMiddleware)  # last: the outermost
```

- **Security headers:** the OWASP REST ones: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cross-Origin-Opener-Policy:
  same-origin`. Headers the app sets itself are never overwritten; `headers=` adds others or
  changes the defaults (None removes one). Add it last, the outermost, so that the answers of
  the middlewares inside it (`BodySizeLimitMiddleware`'s 400 and 413) get them too. Starlette's
  500 for an unhandled exception never does: `ServerErrorMiddleware` wraps every middleware you
  add.
- **HSTS** only on HTTPS requests: browsers ignore it over plain HTTP, and on a development
  `localhost` it would stick. Behind a proxy that terminates TLS, let the server trust it
  (`uvicorn --proxy-headers --forwarded-allow-ips=<proxy address>`).
- **Content-Security-Policy:** `default-src 'none'; frame-ancestors 'none'` for everything that
  isn't HTML (nothing in a JSON response should load anything). HTML responses get `html_csp`,
  None by default, so that the Swagger UI and ReDoc, which load scripts from a CDN, keep working.
- **Body size:** FastAPI has no limit of its own; a client could send an endless body that the app
  reads into memory. A `Content-Length` over the limit gets a 413 before the app runs, a malformed
  or ambiguous one a 400. Bodies without one (chunked) are counted while the app reads them and
  refused once they pass the limit, also when FastAPI parses them into a model. `paths` sets
  other limits by path prefix (the longest wins, None for no limit), matched like the routes,
  without the `root_path`.
- Both are plain ASGI middlewares, not `BaseHTTPMiddleware`: streaming responses still stream,
  and WebSockets go through untouched.
