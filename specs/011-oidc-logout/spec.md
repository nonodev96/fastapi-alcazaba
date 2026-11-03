# Feature Specification: OpenID Connect logout

**Feature Branch**: `feature/oidc-logout` · **Created**: 2026-11-03 · **Status**: Done

**Input**: "Logging out at the identity provider should end the app's sessions, and the app
should be able to log the user out at the provider."

## Clarifications (no question asked; no backward compatibility kept)

- RP-Initiated Logout: `logout_url(id_token_hint, logout_hint, post_logout_redirect_uri, state)`;
  only redirect URIs listed in `post_logout_redirect_uris`. The raw ID token is put in
  `request.state.id_token` at the callback (`_finish` gets the connection).
- Back-Channel Logout: `verify_logout_token()` (2.6 of the specification; `iat` within 5
  minutes), `backchannel_logout_endpoint(UserSessions)`: a `sid` ends that login's sessions only
  (`UserSessions.end_sid`, which indexes sessions by their `oidc_sid`); a `sub` without `sid`
  ends every session of the user. Each token once (its `jti`).
- `private_key_jwt` (RFC 7523) with a `JWTKey`; the client secret is then None.
- CI starts Keycloak with `tests/integration/keycloak/alcazaba-realm.json` (test-only credentials)
  and runs the live tests, RP-Initiated Logout included. Back-channel can't be live-tested (the
  provider would call the app): the fake provider's tokens cover it.
- Out of scope: front-channel logout, PAR, DPoP.

## Requirements

- **FR-001** RP-initiated logout; **FR-002** back-channel logout through UserSessions;
  **FR-003** private_key_jwt; **FR-004** the Keycloak live tests in CI.
