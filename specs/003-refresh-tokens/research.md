# Research: Refresh tokens that rotate and detect reuse

Findings from reading `fastapi_alcazaba/auth/` (`strategies/jwt.py`, `tokens.py`, `apikeys.py`,
`totp.py`), `background/locks.py` and `audit/trail.py`, and RFC 9700 (OAuth 2.0 Security Best
Current Practice, §4.14 refresh token protection), on 2026-11-03.

## R1. Tokens that the backend can't make, and every generation verifiable

- **Decision**: a refresh token is `r1.<family>.<generation>.<mac>`, where `mac` is
  HMAC-SHA256(secret, [family, subject, salt, generation]). The backend keeps, per family, a
  random salt and the current generation, never a token. An exchange recomputes the MAC of the
  presented generation: a match means the token is genuine, whatever its generation.
- **Rationale**:
  - Nothing stored can be turned into a token without the secret (FR-005), like the HMACs of
    `OneTimeCodes`.
  - Every earlier generation is still verifiable without storing its digest, so reuse of any
    ancestor is told apart from a forgery (FR-003 vs FR-004). With random tokens only the last
    ones could be kept, and a forged token naming a family (its id is the access token's `sid`,
    readable by anyone holding one) would have to be either ignored (missing real reuse) or
    treated as reuse (letting anyone log a user out).
  - The newest token can be recomputed for the grace period (R3) without storing it.
- **Secrets**: a list, as elsewhere: the first makes tokens, all verify; a token made with an older
  one exchanges into one made with the first.
- **Alternatives considered**: random tokens with their digests per family (grows with each
  rotation, or forgets ancestors); JWTs as refresh tokens (readable, and revocation still needs
  state).

## R2. Atomic rotation: the family's lock

- **Decision**: an exchange holds the family's lock (`Cache.lock`, waiting up to 5 seconds, no
  renewal) while it reads, checks and writes the family. Two exchanges of one token run one after
  the other; the second finds it one generation behind (R3).
- **Rationale**: every shipped backend holds locks since 0.8 (feature 002). A backend of the app's
  own without them is refused when the component is set up (FR-009).

## R3. The grace period

- **Decision**: a genuine token of the previous generation, presented within `grace` seconds of
  the rotation (and by the same client), gets the current refresh token, recomputed, and a new
  access token; the family doesn't rotate again. After the grace period, or for older generations,
  it's reuse.
- **Rationale**: concurrent tabs end up with one refresh token; no fork. An attacker within those
  seconds gets the token the user also has, so whichever exchanges it second afterwards is caught.

## R4. Revoking access tokens: the `sid` claim and the blocklist

- **Decision**: access tokens carry the family id as `sid` (OpenID Connect's session id).
  `TokenBlocklist.revoke_session(sid, ttl=...)` marks it; `is_revoked(..., session=sid)` checks
  it; `JWTStrategy` passes the token's `sid` when it has one. `RefreshTokens` requires the
  strategy's blocklist.
- **Subjects**: `revoke_subject` writes a cutoff for refresh families (families created before it
  are refused) and calls the blocklist's `revoke_subject` for the access tokens. A family created
  in the same second survives, like the blocklist's tokens: the session that changed the password.

## R5. Client binding

- **Decision**: `issue(..., client=...)` stores the client; an exchange must give the same one. A
  genuine token presented by another client revokes the family (reported as `wrong_client`).
- **Rationale**: the token left its client, which is what reuse detection is for.

## R6. Defaults

- Access tokens 900 s (as `JWTStrategy.issue`); refresh lifetime 30 days; idle 7 days; grace 10 s
  (0 to 60). Common in identity providers (Auth0: 30 days absolute, rotation leeway up to 60 s;
  Okta: 30 s).

## R7. The family record

- Kept under `Cache.state_key("refresh", "family:<sha256 of id>")`, JSON, sealed with the cache's
  `signing_key` or `encryption_key` when there is one (as `TokenBlocklist`), so a planted record is
  ignored. Its expiry is `min(created + lifetime, last use + idle)`, renewed at each exchange.
- Revoking deletes it and marks the session in the blocklist.

## R8. Errors and reporting

- `RefreshRefused(AuthenticationFailed)` with a `reason` (`malformed`, `unknown`, `forged`,
  `expired`, `reused`, `wrong_client`, `revoked`); the app answers 401. Backend failures raise
  `BackendUnavailable` (503 with the app's handler), nothing is issued.
- Metric `refresh` by outcome (issued, rotated, grace and the refusals); audit actions `auth.refresh.issue`,
  `auth.refresh.exchange` (failures with their reason) and `auth.refresh.revoke`; a warning log on
  reuse.
