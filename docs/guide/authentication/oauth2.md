# OAuth2Strategy

Logins with GitHub, Google or any OAuth2 or OpenID Connect provider:
`pip install "fastapi-alcazaba[auth-strategy-oauth2]"`.

In an app: [the example](../../examples/auth_strategies/oauth2.md). How strategies are tried, and the code of a login with GitHub: [Strategies](index.md#strategies).
Every argument: [the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.strategies.oauth2.OAuth2Strategy).

`OAuth2Strategy` logs people in with the authorization code flow and PKCE, for any OAuth2 or
OpenID Connect provider (GitHub, Google, Keycloak…):

- **The login:** `redirect()` sends the browser to the provider with a random `state`; the state
  and the PKCE verifier wait in a cookie for 10 minutes (`__Host-`, HttpOnly, SameSite=Lax).
- **The callback:** the strategy checks the state, so that nobody can log a user in with their
  own account (login CSRF); exchanges the code, with the app's credentials by HTTP Basic
  (`client_auth="post"` in the form); reads `userinfo_url`; and calls `verify(tokens, profile)`.
  It only goes on the callback route: elsewhere it finds nobody.
- **The provider:** its URLs are HTTPS (or `http://localhost`); a provider that doesn't answer is
  a 502.
