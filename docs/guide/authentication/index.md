# Authentication strategies

Who makes a request, told by strategies: JWTs, bearer tokens, sessions, logins with GitHub or
any OAuth2 provider, single sign-on with a SAML identity provider, or your own.

In an app: [the example](../../examples/auth_strategies/index.md). Every class and function: [the API reference](../../reference/authentication.md).

## Strategies

`FastAPIAuth` tells who makes a request by strategies: each strategy knows one way (a JWT, a
session, a login with GitHub…), and a dependency tries those of a route in order. The first one that finds a user gives it to the route; when none
does, a 401. Each strategy has its extra:

```shell
pip install "fastapi-alcazaba[auth-strategy-jwt]"          # JWTStrategy, with PyJWT
pip install "fastapi-alcazaba[auth-strategy-oauth2]"       # OAuth2Strategy, with httpx2
pip install "fastapi-alcazaba[auth-strategy-saml]"         # SAMLStrategy, with signxml
pip install "fastapi-alcazaba[auth-strategy-http-bearer]"  # HTTPBearerStrategy: nothing more
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
credentials into your user can be async or not, and return None to refuse them.

Each strategy has its page:

- [`JWTStrategy`](jwt.md): JWTs, the app's own or an identity provider's.
- [`OAuth2Strategy`](oauth2.md): logins with GitHub, Google or any OAuth2 or OpenID Connect
  provider.
- [`SessionStrategy`](session.md): the user kept in the session at login.
- [`HTTPBearerStrategy`](http-bearer.md): opaque bearer tokens that your function looks up.
- [`SAMLStrategy`](saml.md): single sign-on with a SAML identity provider.
- [Your own strategy](your-own.md): an API key, a client certificate, a signed request…
