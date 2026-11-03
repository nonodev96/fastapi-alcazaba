# OIDCStrategy

!!! warning "Experimental"
    New in 0.6: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../../features/scope.md) lists the tools that are settled.

Logins with an OpenID Connect provider (Google, Microsoft Entra ID, Keycloak, Okta, Auth0,
Zitadel…), whose ID token is verified before your code sees its claims:
`pip install "fastapi-alcazaba[auth-strategy-oidc]"`, which installs httpx2 and PyJWT.

In an app: [the example](../../examples/auth_strategies/oidc.md). How strategies are tried: [Strategies](index.md#strategies). Every argument: [the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.strategies.oidc.OIDCStrategy).

`OIDCStrategy` is [`OAuth2Strategy`](oauth2.md)'s login (the authorization code flow, PKCE and
a `state` in a `__Host-` cookie), plus what OpenID Connect adds: a `nonce`, the provider's
endpoints and keys from its issuer, and the ID token verified as OpenID Connect Core says.

```python
from fastapi_alcazaba.auth.strategies.oidc import OIDCStrategy


def find_or_create_user(claims: dict[str, Any], userinfo: dict[str, Any] | None) -> User | None:
    user_id = f"{claims['iss']}#{claims['sub']}"  # unique and stable; an e-mail address isn't
    return db.users.get(user_id) or db.users.create(user_id, email=claims.get("email"))


google = auth.use(
    OIDCStrategy(
        name="google",
        issuer="https://accounts.google.com",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri="https://example.com/auth/google/callback",
        verify=find_or_create_user,
    )
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await google.discover()  # its endpoints and keys: the app doesn't start without them
    yield
```

- **Discovery:** `await discover()` reads the issuer's `/.well-known/openid-configuration`, whose
  `issuer` must be `issuer` exactly: a document of another issuer would have its tokens accepted
  as this one's. Its endpoints are HTTPS (or `http://localhost`); those given to the strategy
  (`token_url=`, a proxy…) win, and with `authorization_url`, `token_url` and `jwks_url` there's
  nothing to discover.
- **The ID token:** refused unless it's signed by a key of the provider's JWKS with one of
  `algorithms` (RS256, ES256, PS256, EdDSA; never `none`, nor HS256 with the client secret,
  which the provider knows too); its `iss` is the issuer; its `aud` has `client_id`, and its
  `azp` is it when there are several audiences or an `azp`; `exp` hasn't passed and `iat` isn't
  in the future (`leeway`, 60 s); it carries the `nonce` of this login, kept in the state
  cookie; its `at_hash`, when it has one, is the access token's; and with `max_age=600`, its
  `auth_time` is at most 10 minutes old. A provider that sends no ID token is refused.
- **The keys:** fetched from `jwks_uri` and kept for `jwks_lifespan` (an hour). A token whose
  `kid` isn't among them makes them fetched again, the provider having rotated its keys, but at
  most every 30 seconds: made-up `kid`s don't make the app fetch them on every request. Keys for
  encryption (`use: enc`) don't verify signatures.
- **Userinfo:** with `userinfo=True`, the userinfo endpoint is read too, for claims the ID token
  doesn't carry, and refused when its `sub` isn't the ID token's.
- **The user:** `verify(claims, userinfo)` gets the verified claims, and returns your user or
  None to refuse the login; by default the user is the claims. Key users by `iss` and `sub`,
  never by e-mail address, and refuse `email_verified: false` when you use the address.
- **Failures:** each check that fails refuses the login with a 401, the reason in the log (at
  INFO, by `FastAPIAuth`); a provider that doesn't answer is a 502.

`OAuth2Strategy` doesn't verify the `id_token` it may get: for an OpenID Connect provider, use
this one.

## Logging out

Ending the app's session doesn't end the provider's: the next login there goes through without a
password. Two ways to end both, from either side.

```python
from fastapi_alcazaba.security import UserSessions

user_sessions = UserSessions()  # given to ServerSessionMiddleware(users=user_sessions)
keycloak = auth.use(
    OIDCStrategy(
        ...,
        post_logout_redirect_uris=["https://app.example.com/logged-out"],
    )
)


@app.get("/auth/callback")
async def callback(request: Request, user: Annotated[dict, Depends(auth.authenticate("keycloak"))]):
    sessions.login(request, user["sub"])
    request.session["oidc_sid"] = user.get("sid")  # which login: for back-channel logout
    request.session["id_token"] = request.state.id_token  # for logout_url's hint
    ...


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    id_token = request.session.get("id_token")
    clear_session(request)
    url = keycloak.logout_url(
        id_token_hint=id_token, post_logout_redirect_uri="https://app.example.com/logged-out"
    )
    return RedirectResponse(url, status_code=303)


# The provider tells the app when the user logged out there (or an admin ended the session).
app.post("/auth/backchannel-logout")(keycloak.backchannel_logout_endpoint(user_sessions))
```

- **From the app (RP-Initiated Logout):** `logout_url()` is the provider's `end_session_endpoint`
  (from discovery, or `end_session_url=`) with the app's `client_id`, the login's ID token as
  `id_token_hint` (the provider ends that session without asking), and a
  `post_logout_redirect_uri`, which must be one of `post_logout_redirect_uris`, as registered
  there: never one from the request, which would be an open redirect. `state` comes back with it.
  The raw ID token is in `request.state.id_token` once the callback is authenticated.
- **From the provider (Back-Channel Logout):** register the endpoint's URL at the provider. It
  takes the provider's logout token, verifies it (signed by the provider's keys with one of
  `algorithms`, its `iss`, its `aud`, issued in the last 5 minutes, a `jti`, the logout event, a
  `sub` or a `sid`, no `nonce`), takes it once (its `jti`, in the cache's backend), and ends the
  app's sessions of that login: those whose `oidc_sid` is the token's `sid`, or, with a `sub`
  only, every session of the user (`user_id=` maps the provider's `sub` to yours). It answers
  200, or 400 `invalid_request` with the reason in the log, never in the answer.
- **Without a client secret:** `client_auth="private_key_jwt"` with
  `client_key=JWTKey(private_key, algorithm="ES256", kid="app-1")` authenticates to the token
  endpoint with a JWT signed by the app's key (RFC 7523): give the provider the public key (the
  JWKS of a `JWTStrategy(keys=[...])`, `jwks()`), and no secret is shared.
- **Not yet:** front-channel logout (an iframe per app), PAR and DPoP.
