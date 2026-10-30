# Authentication strategies

Who makes a request, told by strategies: JWTs, bearer tokens, sessions, logins with GitHub or
any OAuth2 provider, single sign-on with a SAML identity provider, or your own.

In an app: [the example](../../examples/auth_strategies/index.md). Every class and function: [the API reference](../../reference/authentication.md).

## Strategies

--8<-- "README.md:authentication-strategies"

Each strategy has its page:

- [`JWTStrategy`](jwt.md): JWTs, the app's own or an identity provider's.
- [`OAuth2Strategy`](oauth2.md): logins with GitHub, Google or any OAuth2 or OpenID Connect
  provider.
- [`SessionStrategy`](session.md): the user kept in the session at login.
- [`HTTPBearerStrategy`](http-bearer.md): opaque bearer tokens that your function looks up.
- [`SAMLStrategy`](saml.md): single sign-on with a SAML identity provider.
- [Your own strategy](your-own.md): an API key, a client certificate, a signed request…
