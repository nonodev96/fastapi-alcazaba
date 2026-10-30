# SAMLStrategy

--8<-- "docs/examples/animations/auth_saml.svg"

Single sign-on with a SAML identity provider (Okta, Entra ID, ADFS, Keycloak…), then a session.
The strategy: [SAMLStrategy](../../guide/authentication/saml.md).

## Run it

The app needs an identity provider (an IdP). To try it without one of your own,
[Mock SAML](https://mocksaml.com) takes any e-mail at `example.com` and any password. From a
clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
export IDP_ENTITY_ID=https://saml.example.com/entityid
export IDP_SSO_URL=https://mocksaml.com/api/saml/sso
export IDP_CERTIFICATE="$(curl -s https://mocksaml.com/api/saml/metadata | python -c 'import re, sys, textwrap; c = re.search(r"X509Certificate>([^<]+)<", sys.stdin.read())[1].strip(); print("-----BEGIN CERTIFICATE-----", *textwrap.wrap(c, 64), "-----END CERTIFICATE-----", sep="\n")')"
uvicorn examples.auth_strategies.saml_login:app
```

!!! warning "Mock SAML is for trying, never for an app"

    Its signing key is public, so anyone can sign its answers. With your IdP, set its entity id,
    single sign-on URL and signing certificate, and register the app there with the entity id
    `http://localhost:8000/saml/metadata` and the ACS `http://localhost:8000/saml/acs` (or give
    it `/saml/metadata`).

Set `CACHE_SIGNING_KEY` too (`secrets.token_urlsafe(32)`), the same in every process.

## Try it

`/saml/login` sends the browser to the IdP, with a SAML request. The request's id waits in a
cookie, for the answer to carry:

```console
$ curl -i http://localhost:8000/saml/login
HTTP/1.1 302 Found
location: https://mocksaml.com/api/saml/sso?SAMLRequest=fdFLT8MwDADgv1...
set-cookie: __Host-alcazaba-saml-saml=_b67fcee98db3916463789be0824bd27c7db1ad3a; Max-Age=600; Path=/; HttpOnly; Secure; SameSite=None
cache-control: no-store
```

Open <http://localhost:8000/saml/login> in the browser, and log in at Mock SAML as
`jackson@example.com` with any password. Mock SAML posts its signed answer back to `/saml/acs`;
the strategy checks it, and the app starts a session and takes you to `/me`:

```json
{"id":"saml:jackson@example.com","email":"jackson@example.com"}
```

The IdP reads the app's metadata at <http://localhost:8000/saml/metadata>: its entity id and its
ACS.

## What to look at

- **Checked on what's signed:** the strategy reads only the part that the IdP's signature covers,
  so an answer with a signed part and another one added (signature wrapping) gets nowhere. The
  issuer, the audience (this app), the recipient (the ACS), the request it answers and its
  validity are checked there.
- **Once:** each assertion is kept in the cache's backend until it expires, so a copy posted
  again is refused, by every process.
- **No defaults for the IdP:** without `IDP_ENTITY_ID`, `IDP_SSO_URL` and `IDP_CERTIFICATE` the
  app doesn't start: it never trusts an IdP it wasn't told about.
- **CSRF:** the IdP posts the answer from its own site, so `CSRFMiddleware` lets `/saml/acs`
  through, and the strategy checks what CSRF would: that the answer is for the login this
  browser started.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/saml_login.py"
--8<-- "examples/auth_strategies/saml_login.py"
```
<!-- fmt: on -->
