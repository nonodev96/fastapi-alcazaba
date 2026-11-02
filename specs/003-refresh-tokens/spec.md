# Feature Specification: Refresh tokens that rotate and detect reuse

**Feature Branch**: `feature/refresh-tokens`

**Created**: 2026-11-03

**Status**: Draft

**Input**: User description: "Apps that issue their own JWTs need long-lived sessions without
long-lived access tokens: refresh tokens that rotate on every use and detect theft."

## Clarifications

### Session 2026-11-03

No question was put to the maintainer, who asked for the feature to be built and merged. The
choices a clarification would have asked about are recorded, with their reasons, in research.md:

- Defaults: access tokens 15 minutes, refresh tokens 30 days from the login and 7 days without use,
  a grace period of 10 seconds (R6).
- Within the grace period, the token just exchanged gives back the same new refresh token and a new
  access token: the client and its concurrent requests end with one token, no fork (R3).
- A refresh token presented with another client than the one it was issued to revokes its family,
  like a reuse: a genuine token in another client's hands has left its client (R5).
- The family is the JWT's `sid` claim (OpenID Connect's session id), which `TokenBlocklist` revokes
  (R4).
- A `JWTStrategy` without a blocklist is refused: revoking a family must stop its access tokens
  (R4).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Stay logged in without long-lived access tokens (Priority: P1)

A mobile app logs a user in with `JWTStrategy`. Today it gets one access token: either it lasts
15 minutes and the user logs in again every 15 minutes, or the app makes it last a month and a
stolen token works for a month.

With this feature, the login gives a short access token and a refresh token. When the access token
expires, the app exchanges the refresh token for a new pair; the refresh token it used stops
working.

**Why this priority**: it's the feature: without it the other stories have nothing to protect.

**Independent Test**: log in, exchange the refresh token twice, call the API with each access
token.

**Acceptance Scenarios**:

1. **Given** a login, **When** the app issues tokens, **Then** it gets an access token, a refresh
   token and their lifetimes.
2. **Given** a refresh token, **When** it's exchanged, **Then** a new access token and a new
   refresh token come back, both for the same user and with the same claims.
3. **Given** a refresh token older than the absolute lifetime, or unused for longer than the idle
   lifetime, **When** it's exchanged, **Then** it's refused.

---

### User Story 2 - A stolen refresh token is noticed (Priority: P1)

An attacker copies a user's refresh token. Whoever exchanges it second (the attacker or the user)
presents a token that was already exchanged.

With this feature, that second exchange revokes every token of the login (refresh and access
tokens), is refused, and is reported in the logs, the metrics and the audit trail. The user logs in
again; the attacker is out.

**Why this priority**: rotation alone only helps if reuse is caught.

**Independent Test**: exchange a token, wait past the grace period, exchange it again: refused,
and the new tokens stop working too.

**Acceptance Scenarios**:

1. **Given** a refresh token already exchanged, **When** it's exchanged again after the grace
   period, **Then** it's refused and the whole family is revoked: its newest refresh token is
   refused and its access tokens are refused by `JWTStrategy`.
2. **Given** a forged refresh token (a family id seen in an access token, a guessed rest),
   **When** it's exchanged, **Then** it's refused and nothing is revoked.
3. **Given** a refresh token bound to a client, **When** another client presents it, **Then** it's
   refused and its family revoked.

---

### User Story 3 - Concurrent refreshes don't log the user out (Priority: P2)

A web app opens three tabs; their access tokens expire together and each tab exchanges the same
refresh token at the same time.

With this feature, within a short grace period the same refresh token exchanged again gives the
same new refresh token (and a new access token) instead of counting as theft.

**Why this priority**: without it, rotation logs out ordinary users; but it only matters once
rotation exists.

**Acceptance Scenarios**:

1. **Given** five exchanges of one refresh token at once, **When** they run, **Then** all succeed
   and return the same new refresh token.
2. **Given** an exchange, **When** the old token comes back within the grace period, **Then** it
   gets the newest refresh token; after it, it's reuse.

---

### User Story 4 - Logging out (Priority: P2)

The user logs out on one device, or everywhere after a password change, or an operator revokes a
login.

**Acceptance Scenarios**:

1. **Given** a refresh token, **When** it's revoked (logout), **Then** its family's refresh and
   access tokens are refused.
2. **Given** a subject, **When** all their tokens are revoked, **Then** every family issued before
   is refused, and the access tokens too; a login right after works.
3. **Given** a family id (the `sid` of an access token), **When** it's revoked, **Then** as 1.

---

### Edge Cases

- A malformed or oversized refresh token: refused, never an error.
- The backend fails: the exchange fails closed (BackendUnavailable), nothing is issued.
- A family revoked while one of its exchanges runs: that exchange either finishes before the
  revocation or is refused; never a token that outlives it.
- Secrets rotated: refresh tokens made with an older secret still exchange, and get one made with
  the newest.
- A backend without locks (one written by the app): refused when set up.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Issuing MUST return an access token (from the `JWTStrategy`, with a `sid` claim) and
  an opaque refresh token, with their lifetimes.
- **FR-002**: Exchanging a refresh token MUST return a new pair and make the presented token
  unusable (but for FR-006), atomically: two exchanges of one token never yield two families.
- **FR-003**: An exchanged refresh token presented again after the grace period MUST revoke its
  family and be refused, reported as reuse (log warning, metric, audit).
- **FR-004**: A refresh token that isn't genuine MUST be refused without revoking anything.
- **FR-005**: Refresh tokens MUST NOT be stored in clear, nor anything that lets the backend's
  reader make one; they MUST expire after an absolute and an idle lifetime; they're bound to the
  subject, and to a client when one is given.
- **FR-006**: Within the grace period, the token just exchanged MUST give the newest refresh token
  of its family and a new access token.
- **FR-007**: Revoking a family, a subject or the family of a given refresh token MUST refuse its
  refresh tokens and, through the strategy's `TokenBlocklist`, its access tokens until they expire.
- **FR-008**: Issue, exchange, reuse and revocation MUST be recorded in the audit trail when one is
  given.
- **FR-009**: The component MUST refuse, when set up, a strategy that can't sign, a strategy
  without a blocklist, and a backend without locks.
- **FR-010**: `TokenBlocklist` MUST revoke a session id (`revoke_session`), and `JWTStrategy` MUST
  refuse tokens whose `sid` is revoked.

### Key Entities

- **Family**: one login; a random id (the `sid`), a random salt, the subject, the client, the
  claims, the creation time, the generation, when it was last rotated and used.
- **Refresh token**: `r1.<family>.<generation>.<mac>`; the MAC (HMAC-SHA256 with the secret) binds
  the family, its subject, its salt and the generation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Unit tests for rotation, reuse, grace, expiry, client binding and every revocation;
  live tests on every backend for rotation, concurrent exchanges and reuse.
- **SC-002**: Five concurrent exchanges of one token on a live backend all return the same new
  refresh token.
- **SC-003**: The JWT guide has a "Refresh tokens" section, and the JWT example issues, exchanges
  and revokes them, tested.

## Assumptions

- The app keeps the refresh token where its platform keeps secrets (an HttpOnly cookie, the
  keychain); the guide says so, nothing enforces it.
- Access tokens are short (minutes): revocation reaches them through the blocklist anyway.

## Out of Scope

- Acting as an OAuth2 authorization server for other apps (token endpoint for clients, consent).
- Sender-constrained tokens (DPoP, mTLS).
