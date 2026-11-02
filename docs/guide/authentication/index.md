# Authentication strategies

Who makes a request, told by strategies: JWTs, bearer tokens, sessions, logins with GitHub or
any OAuth2 provider, logins with an OpenID Connect provider whose ID token is verified, single
sign-on with a SAML identity provider, API keys kept by Unkey, or
your own.

In an app: [the example](../../examples/auth_strategies/index.md). Every class and function: [the API reference](../../reference/authentication.md).

## Strategies

`FastAPIAuth` tells who makes a request by strategies: each strategy knows one way (a JWT, a
session, a login with GitHub…), and a dependency tries those of a route in order. The first one that finds a user gives it to the route; when none
does, a 401. Each strategy has its extra:

```shell
pip install "fastapi-alcazaba[auth-strategy-jwt]"          # JWTStrategy, with PyJWT
pip install "fastapi-alcazaba[auth-strategy-oauth2]"       # OAuth2Strategy, with httpx2
pip install "fastapi-alcazaba[auth-strategy-oidc]"         # OIDCStrategy, with httpx2 and PyJWT
pip install "fastapi-alcazaba[auth-strategy-saml]"         # SAMLStrategy, with signxml
pip install "fastapi-alcazaba[auth-strategy-http-bearer]"  # HTTPBearerStrategy: nothing more
pip install "fastapi-alcazaba[auth-strategy-unkey]"        # UnkeyStrategy, with httpx2
```

```python
from fastapi_alcazaba.auth import FastAPIAuth, SessionStrategy
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy
from fastapi_alcazaba.auth.strategies.oauth2 import OAuth2Strategy

auth = FastAPIAuth()
github = auth.use(
    OAuth2Strategy(
        name="github",
        authorization_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        redirect_uri="https://example.com/auth/github/callback",
        verify=find_or_create_user,  # (tokens, profile) -> your user
    )
)
sessions = auth.use(SessionStrategy(load_user))  # load_user(user_id) -> your user
tokens = auth.use(JWTStrategy(settings.jwt_secret, verify=lambda c: load_user(c["sub"])))

CurrentUser = Annotated[User, Depends(auth.authenticate("session", "jwt"))]
GitHubUser = Annotated[User, Depends(auth.authenticate("github"))]


@app.get("/me")
async def me(user: CurrentUser) -> User:  # a browser's session, or a script's token
    return user


@app.get("/login/github")
async def login() -> RedirectResponse:
    return github.redirect()  # to GitHub, which sends the user back to the callback


@app.get("/auth/github/callback")
async def callback(request: Request, user: GitHubUser) -> RedirectResponse:
    sessions.login(request, user.id)
    return RedirectResponse("/me", status_code=303)
```

The strategies of a dependency are tried in order. Each returns the user; None when the request
doesn't carry its credentials; or raises `AuthenticationFailed` when they're there but wrong
(logged, never sent). Either way the next one is tried. When none finds a user, a 401 with the
`WWW-Authenticate` challenges of those tried, a WebSocket is closed with 1008, and
`optional=True` gives None. Any other error is a 500, never a request let through. Without a
dependency: `await auth.user(connection)`. The `verify` and `load_user` functions that turn
credentials into your user can be async or not, and return None to refuse them. A request is
authenticated once: the dependencies of a route that ask for the same strategies get the user
the first one found, so a token is decoded, and an API key verified, once.

### Scopes

Routes that need scopes ask for them the FastAPI way, with `Security`, and the OpenAPI schema
shows them:

```python
from fastapi import Security

Writer = Annotated[User, Security(auth.authenticate("jwt"), scopes=["orders:write"])]


@app.post("/orders")
async def place_order(order: Order, user: Writer) -> Order: ...


@app.delete("/orders/{order_id}", dependencies=[Depends(auth.authenticate(scopes=["admin"]))])
async def delete_order(order_id: int) -> None: ...
```

- **Granted by the strategy that found the user:** `JWTStrategy` grants the token's `scope` (a
  string separated by spaces, RFC 9068) and `scp` (a list: Entra ID, Okta); `scope_claims=` names
  others (Auth0's `permissions`). `HTTPBearerStrategy` grants what `scopes_of(user)` gives. Your
  own strategy calls `self.grant(connection, scopes)`. Sessions and logins grant none: a route
  that needs scopes refuses them. `granted_scopes(request)` tells a route what was granted.
- **A missing scope** is a 403 with `WWW-Authenticate: Bearer error="insufficient_scope",
  scope="orders:write"` (RFC 6750) for Bearer strategies; a WebSocket is closed with 1008.
- **API keys** of `FastAPIAPIKeys` have scopes of their own: `keys.require("orders:read")`.
  Unkey's have their permissions, which `UnkeyStrategy(permissions=...)` asks Unkey about.

### In the audit trail

`FastAPIAuth(audit=audit)` sends a [`FastAPIAudit`](../audit.md) what security reviews ask for,
with the request's route, client and ID:

- **`auth.failure`:** credentials there but wrong, with the strategy; never the reason's text,
  which may carry what the client sent.
- **`auth.login`:** a login by OAuth2, OpenID Connect or SAML, with who logged in.
- **`auth.scope`:** a request without a scope it needed, with the scopes missing.

Each request authenticated isn't recorded, nor those without credentials: they'd bury the rest.
`actor=` tells who a user is (its ID); by default the user itself when it's an ID, else its
`sub` or `id`.

Each strategy has its page:

- [`JWTStrategy`](jwt.md): JWTs, the app's own or an identity provider's.
- [`OAuth2Strategy`](oauth2.md): logins with GitHub or any OAuth2 provider.
- [`OIDCStrategy`](oidc.md): logins with an OpenID Connect provider (Google, Entra ID, Keycloak,
  Okta…), its ID token verified.
- [`SessionStrategy`](session.md): the user kept in the session at login.
- [`HTTPBearerStrategy`](http-bearer.md): opaque bearer tokens that your function looks up.
- [`SAMLStrategy`](saml.md): single sign-on with a SAML identity provider.
- [`UnkeyStrategy`](unkey.md): API keys that Unkey keeps, with their permissions, credits and
  rate limits.
- [Your own strategy](your-own.md): an API key, a client certificate, a signed request…
