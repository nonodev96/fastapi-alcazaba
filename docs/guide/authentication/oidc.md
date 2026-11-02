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
