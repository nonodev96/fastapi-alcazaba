"""A small shop rendered on the server, for browsers: a session cookie, CSRF protection and the
security headers.

    uvicorn examples.browser_app.main:app

Then open http://localhost:8000 and sign in as "ana", password "correct horse battery staple".
The cookies are Secure; browsers accept them from http://localhost, which they treat as secure.

- `ServerSessionMiddleware` keeps each session (who signed in, the cart) in the cache's backend;
  the cookie only carries a random id. Signing in gives a new id, signing out deletes the
  session, and 30 minutes without requests end it.
- `CSRFMiddleware` refuses what pages on other sites make the browser send. With
  `require_token=True` it also wants the page's token in the `X-CSRF-Token` header: app.js sends
  the forms with it.
- `SecurityHeadersMiddleware` adds the security headers, with a Content-Security-Policy that only
  lets the pages run the app's own script; `BodySizeLimitMiddleware` refuses large bodies.

The sessions live in memory here; in production, in Redis or Valkey, so that every process sees
them, and a sign-out at once. Set SESSION_SIGNING_KEY (secrets.token_urlsafe(32)), in the
environment or a .env file.
"""

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from html import escape
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.concurrency import run_in_threadpool

from fastapi_alcazaba import FastAPICache, FastAPIRateLimiter
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.csrf import CSRFMiddleware
from fastapi_alcazaba.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from fastapi_alcazaba.sessions import ServerSessionMiddleware, clear_session, rotate_session

# Only the app's own script, styles and images; no plugins, no framing, forms only to the app.
CSP = "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"

APP_JS = """\
// Sends the forms with fetch, adding the CSRF token in the header that CSRFMiddleware checks:
// a plain form can't set headers.
const token = document.querySelector('meta[name="csrf-token"]').content;
for (const form of document.querySelectorAll("form[method=post]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await fetch(form.action, {
      method: "POST",
      headers: {"X-CSRF-Token": token},
      body: new URLSearchParams(new FormData(form)),
    });
    if (response.redirected) location.assign(response.url);
    else alert(`Refused: ${response.status} ${response.statusText}`);
  });
}
"""


class Product(StrEnum):
    # An enum: the cart only holds what the shop sells, so sessions stay small.
    OLIVE_OIL = "Olive oil"
    ALMONDS = "Almonds"
    FIGS = "Figs"


def scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


class Users:
    """Stands in for the users table: a salt and a slow hash of each password, never the
    password itself."""

    def __init__(self) -> None:
        salt = secrets.token_bytes(16)
        self.rows = {"ana": (salt, scrypt("correct horse battery staple", salt))}

    def check(self, username: str, password: str) -> bool:
        # Hashed for unknown users too: the time it takes doesn't tell who has an account.
        salt, stored = self.rows.get(username, (b"\0" * 16, b""))
        return hmac.compare_digest(scrypt(password, salt), stored)


users = Users()


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    session_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production. Signed: whoever can write to the backend can't forge a
    # session (encryption_key instead, with the crypto extra, if they hold personal data).
    FastAPICache.init(InMemoryBackend(), prefix="shop", signing_key=settings.session_signing_key)
    yield


# No /docs: a site doesn't need them, and their CDN scripts wouldn't pass the CSP.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Each middleware added wraps the ones before it: the first is the innermost.
app.add_middleware(
    ServerSessionMiddleware,
    cookie_name="__Host-session",  # a sibling subdomain can't plant one
    max_age=8 * 3600,
    idle_timeout=30 * 60,
)
app.add_middleware(CSRFMiddleware, require_token=True)  # outside: refusals don't read sessions
app.add_middleware(BodySizeLimitMiddleware, max_size=64 * 1024)  # forms are small
# The outermost: the refusals of the middlewares inside it get the headers too.
app.add_middleware(SecurityHeadersMiddleware, html_csp=CSP)


def page(request: Request, body: str) -> HTMLResponse:
    """The page around `body`, with the CSRF token for app.js."""
    token = escape(request.state.csrf_token)
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Shop</title>'
        f'<meta name="csrf-token" content="{token}"><script src="/app.js" defer></script>'
        f"</head><body>{body}</body></html>"
    )


def form(action: str, button: str, **fields: str) -> str:
    inputs = "".join(
        f'<input type="hidden" name="{name}" value="{escape(value)}">'
        for name, value in fields.items()
    )
    return f'<form method="post" action="{action}">{inputs}<button>{button}</button></form>'


@app.get("/")
async def home(request: Request) -> HTMLResponse:
    user = request.session.get("user")
    if user is None:
        header = '<p><a href="/login">Sign in</a></p>'
    else:
        header = f"<p>Signed in as {escape(user)}.</p>{form('/logout', 'Sign out')}"
    products = "".join(
        f"<li>{escape(product)} {form('/cart', 'Add to cart', product=product)}</li>"
        for product in Product
    )
    cart = request.session.get("cart", {})
    items = "".join(f"<li>{escape(p)}: {cart[p]}</li>" for p in Product if p in cart)
    return page(request, f"{header}<h1>Shop</h1><ul>{products}</ul><h2>Cart</h2><ul>{items}</ul>")


@app.get("/login")
async def login_page(request: Request, failed: bool = False) -> HTMLResponse:
    error = "<p>Wrong username or password.</p>" if failed else ""
    return page(
        request,
        f"<h1>Sign in</h1>{error}"
        '<form method="post" action="/login">'
        '<label>Username <input name="username" autocomplete="username" required></label> '
        '<label>Password <input name="password" type="password" '
        'autocomplete="current-password" required></label> <button>Sign in</button></form>',
    )


# Five attempts a minute per client address.
@app.post("/login", dependencies=[Depends(FastAPIRateLimiter(times=5, minutes=1))])
async def login(
    request: Request,
    username: Annotated[str, Form(max_length=100)],
    password: Annotated[str, Form(max_length=200)],
) -> RedirectResponse:
    # In a thread: scrypt is slow on purpose, and would hold up the other requests.
    if not await run_in_threadpool(users.check, username, password):
        return RedirectResponse("/login?failed=true", status_code=303)
    request.session["user"] = username
    rotate_session(request)  # a new id: one planted in the browser before is worthless now
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    clear_session(request)  # deleted from the backend: a copied cookie is worth nothing now
    return RedirectResponse("/", status_code=303)


@app.post("/cart")
async def add_to_cart(request: Request, product: Annotated[Product, Form()]) -> RedirectResponse:
    cart = request.session.setdefault("cart", {})
    cart[product.value] = min(cart.get(product.value, 0) + 1, 10)  # at most 10 of each
    return RedirectResponse("/", status_code=303)


@app.get("/app.js")
async def script() -> Response:
    return Response(APP_JS, media_type="text/javascript")


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
