# SAMLStrategy

Single sign-on with a SAML identity provider: `pip install "fastapi-alcazaba[auth-strategy-saml]"`.

In an app: [the example](../../examples/auth_strategies/saml.md). How strategies are tried: [Strategies](index.md#strategies). Every argument: [the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.strategies.saml.SAMLStrategy).

`SAMLStrategy` is SAML 2.0 single sign-on with an identity provider (Okta, Entra ID, ADFS,
Keycloak…):

- **The login:** `redirect()` sends the browser to the IdP with an AuthnRequest; the IdP posts its
  answer to the ACS route.
- **The answer:** the strategy verifies its XML signature with the IdP's certificate (signxml,
  SHA-2 only, no DTDs). Then, on what the signature covers and nothing else (signature
  wrapping), it checks the issuer, the audience (`entity_id`), the recipient (`acs_url`), the
  request it answers and when it's valid.
- **Once:** each assertion works once: it's kept in the cache's backend until it expires, and
  logins are refused when the backend fails.
- **Metadata:** `metadata()` is the app's metadata, for the IdP.
- **Not supported:** logins the IdP starts, encrypted assertions, signed requests, single logout.

The SAML request's id waits in a `SameSite=None` cookie, because the IdP posts the answer from
its own site; for the same reason `CSRFMiddleware` must let the ACS through:

```python
from fastapi_alcazaba.auth.strategies.saml import SAMLStrategy

saml = auth.use(
    SAMLStrategy(
        entity_id="https://example.com/saml/metadata",  # this app, at the IdP
        acs_url="https://example.com/saml/acs",
        idp_entity_id=settings.idp_entity_id,
        idp_sso_url=settings.idp_sso_url,
        idp_certificate=settings.idp_certificate,  # PEM; a list while it rotates
        verify=find_or_create_user,  # (SAMLProfile) -> your user
    )
)
SAMLUser = Annotated[User, Depends(auth.authenticate("saml"))]
app.add_middleware(CSRFMiddleware, exempt_paths=["/saml/acs"])  # the IdP posts it


@app.get("/saml/login")
async def saml_login() -> RedirectResponse:
    return saml.redirect()


@app.post("/saml/acs")
async def saml_acs(request: Request, user: SAMLUser) -> RedirectResponse:
    sessions.login(request, user.id)
    return RedirectResponse("/me", status_code=303)


@app.get("/saml/metadata")
async def saml_metadata() -> Response:
    return Response(saml.metadata(), media_type="application/samlmetadata+xml")
```
