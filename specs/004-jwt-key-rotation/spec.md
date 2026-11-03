# Feature Specification: JWT signing keys of any type that rotate, and robust JWKS

**Feature Branch**: `feature/jwt-key-rotation`

**Created**: 2026-11-03

**Status**: Draft

**Input**: User description: "JWTStrategy should rotate signing keys of any type without downtime,
and fetch remote verification keys as robustly as the OIDC strategy does."

## Clarifications

### Session 2026-11-03

No question was put to the maintainer, who asked for the feature to be built and merged. The
choices a clarification would have asked about are recorded, with their reasons, in research.md:

- Keys are `JWTKey` objects, each with one algorithm; the first one signs. HMAC and asymmetric
  keys may be mixed in `keys=`, since each is pinned to its algorithm: that's how an app moves
  from HS256 to RS256 (R1, R5).
- A key's id (`kid`) is given or derived: the RFC 7638 thumbprint of a public key, an HMAC of a
  constant for a secret, so the header reveals nothing about it (R2).
- A token without a `kid`, or with one no key has, is tried with the keys whose `kid` wasn't
  given, as before: tokens issued by earlier versions keep working (R3).
- A retired key verifies until its `until` date, then its tokens are refused (R4).
- When the provider can't be reached, the keys already fetched are used for `jwks_stale` seconds
  (1 hour) past their lifespan; then tokens are refused with a 503, never accepted (R7).
- The JWKS URL must be HTTPS (or http://localhost), as OIDCStrategy requires (R7).
- `signing_key=` is deprecated in favour of `keys=` (R6).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Rotate an asymmetric signing key without logging anyone out (Priority: P1)

An app signs its tokens with RS256 (or ES256, EdDSA) so that other services can verify them with
the public key. Today, giving a second key raises "only HMAC secrets rotate": replacing the key
refuses every token issued with the old one, and every user logs in again.

With this feature, the app lists its keys: the new one first, which signs; the old one after,
which verifies until a date. Tokens name their key (`kid`), and verification uses that key.

**Why this priority**: it's the feature.

**Independent Test**: issue a token with key A; configure [B, A]; the token verifies, new tokens
are signed with B; after A's date, A's token is refused.

**Acceptance Scenarios**:

1. **Given** keys [B, A] of any types, **When** a token is issued, **Then** it's signed with B and
   carries B's `kid`.
2. **Given** a token signed with A, **When** it's verified with [B, A], **Then** it's accepted
   until A's `until`, and refused after.
3. **Given** keys [RS256 key, HS256 secret], **When** tokens of each arrive, **Then** each is
   verified with its key and algorithm only.

---

### User Story 2 - An identity provider's keys don't slow down or open the API (Priority: P1)

An API verifies an identity provider's tokens with its JWKS. Today the keys are fetched with a
blocking client and a 30-second timeout: when the provider is slow, every request with an unknown
key waits; a flood of tokens with made-up `kid`s makes the app fetch the JWKS each time.

With this feature, the keys are fetched asynchronously with a short timeout and a circuit
breaker, kept, refetched for an unknown `kid` at most every 30 seconds, and concurrent refetches
are one.

**Why this priority**: an outage of the provider shouldn't become the API's.

**Acceptance Scenarios**:

1. **Given** a JWKS, **When** tokens with its `kid`s arrive, **Then** it's fetched once and kept
   for `jwks_lifespan`.
2. **Given** tokens with an unknown `kid`, **When** many arrive at once and over time, **Then** the
   JWKS is fetched at most once per 30 seconds, and they're refused (401).
3. **Given** the provider rotated its key, **When** a token with the new `kid` arrives, **Then**
   the JWKS is fetched again and the token accepted.
4. **Given** a provider that doesn't answer within the timeout, **When** keys were fetched less
   than `jwks_lifespan + jwks_stale` ago, **Then** they're still used; **When** there are none,
   **Then** the request gets a 503, and the breaker stops calling it for a while.

---

### User Story 3 - Publish the app's public keys (Priority: P3)

Another service verifies the app's tokens. The app serves its public keys as a JWKS, with the
same `kid`s its tokens carry, including the retiring ones.

**Acceptance Scenarios**:

1. **Given** keys [B, A] asymmetric and an HMAC secret, **When** `jwks()` is called, **Then** it
   gives B and A's public keys with their `kid`, `alg` and `use`, never the secret nor a private
   part.

### Edge Cases

- Two keys with one `kid`: refused at setup.
- The first key with an `until`: refused (the key that signs doesn't retire).
- A JWKS with an HMAC (`oct`) key, an encryption key, or a key whose `alg` differs from the
  token's: that key isn't used.
- A JWKS that isn't one (HTML, an error, too large): a failed fetch, as a timeout.
- A token whose header names an algorithm its key isn't pinned to, or `none`: refused.
- A public key given as `key=` with an HMAC algorithm: refused (as today).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `JWTStrategy` MUST accept several keys (HMAC, RSA, RSA-PSS, EC, EdDSA), each pinned
  to one algorithm; the first signs.
- **FR-002**: Issued tokens MUST carry the signing key's `kid`; verification MUST use the key
  named by the token's `kid`, and only with that key's algorithm.
- **FR-003**: A key MAY have an `until` date after which its tokens are refused.
- **FR-004**: Tokens without a `kid`, or with an unknown one, MUST still verify with the keys
  whose `kid` wasn't given (compatibility with tokens issued before).
- **FR-005**: Remote JWKS MUST be fetched asynchronously, with a timeout (3 s by default) and a
  circuit breaker per origin, kept for `jwks_lifespan`, refetched for an unknown `kid` at most
  every 30 s, concurrent refetches merged into one; counted in the metrics.
- **FR-006**: When no usable key can be had, tokens MUST be refused: 503 when the provider is
  unreachable, 401 for a `kid` it doesn't have.
- **FR-007**: `alg: none`, an HMAC key for an asymmetric token or the reverse, and HMAC keys from a
  JWKS MUST never verify a token.
- **FR-008**: The existing constructor arguments MUST keep working; `signing_key=` warns that it's
  deprecated.
- **FR-009**: `jwks()` MUST give the public keys of the asymmetric keys, never secrets or private
  parts.

### Key Entities

- **JWTKey**: a key, its algorithm, its `kid` (given or derived), its `until`.
- **Remote key set**: the JWKS fetched, when, and when it was last attempted.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Rotation tests across HS256, RS256, PS256, ES256 and EdDSA, and mixed.
- **SC-002**: JWKS tests with a fake server: timeouts, unknown `kid`, refetch rate, concurrent
  refetches, stale keys, 503.
- **SC-003**: The JWT guide explains rotation, retirement and publishing keys.

## Assumptions

- `until` is set to the rotation time plus the longest lifetime of the tokens the old key signed.
- Apps call `aclose()` at shutdown when the strategy made its own HTTP client.

## Out of Scope

- A JWKS endpoint served by the library (the guide shows the route with `jwks()`).
- Keys loaded from a KMS or an HSM.
