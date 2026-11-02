# OIDCStrategy

--8<-- "docs/examples/animations/auth_oidc.svg"

People log in with an OpenID Connect provider (Keycloak, Google, Microsoft Entra ID, Okta…):
the authorization code flow with PKCE and a nonce, the ID token verified, then a session that
remembers them. The strategy: [OIDCStrategy](../../guide/authentication/oidc.md).

## Run it

Register the app at the provider, with the redirect URI `http://localhost:8000/auth/callback`.
With Keycloak, in a realm: Clients → Create client, client authentication on, the standard flow,
and that redirect URI. Then, from a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
export OIDC_ISSUER=http://localhost:8080/realms/example OIDC_CLIENT_ID=... OIDC_CLIENT_SECRET=...
uvicorn examples.auth_strategies.oidc_login:app
```

Set `CACHE_SIGNING_KEY` too (`secrets.token_urlsafe(32)`), the same in every process. At start,
the app reads the issuer's discovery document, for the provider's endpoints and keys: it doesn't
start when the issuer doesn't answer, or its document is another issuer's.

## Try it

Open <http://localhost:8000/login> in the browser. `/login` sends it to the provider's login
page, with a random state, a PKCE challenge and a nonce, which wait in a cookie for the callback
(`__Host-alcazaba-oauth2-oidc`: the state, the PKCE verifier and the nonce). Against Keycloak 26.4,
with a realm `alcazaba` at `http://localhost:18090`:

```text
302 → http://localhost:18090/realms/alcazaba/protocol/openid-connect/auth
      ?response_type=code&client_id=alcazaba-example&redirect_uri=...%2Fauth%2Fcallback
      &state=...&code_challenge=...&code_challenge_method=S256&scope=openid+email+profile&nonce=...
```

Log in at the provider. It sends you back to the callback, where the strategy checks the state,
exchanges the code, verifies the ID token (Keycloak signs it with RS256, by a key of its JWKS) and
calls `find_or_create_user`; the app starts a session and takes you to `/me`:

```json
{"id":"http://localhost:18090/realms/alcazaba#165fb2e0-1057-4b2c-b927-2c0ca4369d08","email":"ana@example.com","name":"Ana Test"}
```

The same callback again (its code, from the browser's history) gets a 401: Keycloak refuses a
code used once already.

## What to look at

- **The ID token is verified:** its signature with the provider's keys, its issuer, its audience
  (this app), its expiry and the login's nonce, before `find_or_create_user` sees its claims. A
  token for another app, or from another login, is refused.
- **Users by issuer and subject:** `sub` is unique and stable for each issuer; an e-mail address
  can change hands. An address the provider says nobody proved (`email_verified: false`) refuses
  the login.
- **Discovered at start:** the endpoints and the keys come from the issuer, in the lifespan; the
  keys are fetched again when the provider rotates them.
- **Only on the callback:** `LoggedIn` is the only dependency with the `oidc` strategy; the
  session tells who's there everywhere else, and `CSRFMiddleware` refuses what other sites make
  browsers send with its cookie.

## The code

<!-- fmt: off -->
```python title="examples/auth_strategies/oidc_login.py"
--8<-- "examples/auth_strategies/oidc_login.py"
```
<!-- fmt: on -->
