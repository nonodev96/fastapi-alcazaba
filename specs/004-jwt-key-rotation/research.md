# Research: JWT signing keys of any type that rotate, and robust JWKS

Findings from reading `fastapi_alcazaba/auth/strategies/jwt.py`, `oidc.py`, `oauth2.py`,
`authz/opa.py`, `outbound/transports.py`, PyJWT 2.15 (`api_jws.py`, `api_jwk.py`,
`algorithms.py`) and RFC 7517, 7518 and 7638, on 2026-11-03.

## R1. One key, one algorithm: `JWTKey`

- **Decision**: `JWTKey(key, algorithm=..., kid=None, until=None)`. `key` is an HMAC secret (at
  least 32 bytes), a private key (it signs, its public part verifies) or a public key (it only
  verifies). `JWTStrategy(keys=[...])`: the first one signs.
- **Rationale**: pinning the algorithm to the key makes algorithm confusion impossible by
  construction: the token's `alg` must be its key's (PyJWT does the same for `PyJWK`). The private
  key's public part is derived, because PyJWT can't verify with a private key.
- **Alternatives considered**: lists of keys and of algorithms side by side (the old shape: one
  algorithm family for every key); dicts (untyped).

## R2. Key ids

- **Decision**: the `kid` given, or derived: for an asymmetric key, its RFC 7638 thumbprint
  (SHA-256 of the public JWK's required members), for a secret the first 22 characters of
  base64url(HMAC-SHA256(secret, "fastapi-alcazaba jwt kid")).
- **Rationale**: a thumbprint is what other verifiers can compute too; an HMAC of a constant names
  a secret without revealing anything of it (its hash would, for a weak one).

## R3. Choosing the key of a token

- **Decision**: a token whose `kid` is a key's is verified with that key only. A token without a
  `kid`, or with one no key has, is tried with the keys whose `kid` wasn't given, in turn. The
  header's `alg` must be one of the key's algorithms.
- **Rationale**: tokens issued by earlier versions have no `kid`, and an identity provider's
  tokens verified with a static public key carry the provider's own `kid`, not the thumbprint:
  both keep working. An app that gives every `kid` gets unknown `kid`s refused outright.

## R4. Retiring a key

- **Decision**: `until` (an aware datetime): past it, the key verifies nothing. The first key
  can't have one.
- **Rationale**: one deploy does the rotation (new key first, old key with `until`); removing the
  old key later is cleanup, not a deadline.

## R5. Mixing HMAC and asymmetric keys

- **Decision**: allowed in `keys=`; still refused for `key=` with `algorithms=`.
- **Rationale**: with each key pinned, a public key can never be used as an HMAC secret. And it
  is the way from HS256 to RS256: [RS256 key, HS256 secret until ...].

## R6. The old arguments

- **Decision**: `key=` (one secret or public key, a list of HMAC secrets) and `algorithms=` keep
  working; they become keys whose `kid` wasn't given, accepting every algorithm of `algorithms`
  (one family, as before). `signing_key=` warns `DeprecationWarning`: `keys=` says the same.
  `algorithms=` with `keys=` is refused: each key has its own.

## R7. Remote JWKS

- **Decision**:
  - Fetched with httpx2 through `ResilientTransport` (deadline `jwks_timeout`, 3 s; a breaker per
    origin; at most 256 KiB), or the `jwks_client` given. httpx2 joins the `auth-strategy-jwt`
    extra.
  - The URL is HTTPS, or http to localhost (`_secure`, as OIDCStrategy).
  - Kept for `jwks_lifespan` (300 s); a `kid` not among them makes a refetch, at most every
    `JWKS_REFETCH` (30 s, OIDCStrategy's); a failed fetch isn't retried sooner either.
  - One fetch at a time (a lock); requests that waited for it use its result.
  - A failed fetch (no answer, not 200, not a JWKS with a usable key) keeps the keys there were
    for `jwks_stale` (3600 s) past their lifespan; without usable keys, `JWKSUnavailable`, which
    `authenticate` turns into a 503 (WebSocket 1013): fail closed, but not a "log in again".
  - Keys used: `use` absent or `sig`, never `oct`; a JWK's `alg` pins it; without one, the
    token's `alg` (among `algorithms`) must fit its type. `algorithms` can't be HMAC with a JWKS;
    by default they're OIDCStrategy's (RS256, ES256, PS256, EdDSA).

## R10. No fuzz target

- **Decision**: the JWKS parser (`_usable_keys`) gets a property test with hypothesis in the unit
  tests, not a fuzz target: ClusterFuzzLite's image has no PyJWT (its requirements are the core
  package's), and a JWKS comes from the provider, not from clients. Tokens' headers, which do,
  are parsed by PyJWT before the strategy sees them.
- **Rationale**: the same deadline, breaker and metrics as OPA and Unkey; the stale keys keep an
  API up while its provider is down (the keys rarely change; a key the provider withdrew is still
  accepted at most `jwks_stale` longer).
- **Alternatives considered**: keeping `PyJWKClient` in a thread (blocking, 30 s, no breaker, no
  stale keys).

## R8. Metrics

- **Decision**: counter `auth.jwks` by strategy and outcome: `fetched`, `failed` (whether older
  keys were kept shows in the 503s that follow, or not). The transport counts its breaker
  (`outbound.breaker`).

## R9. Publishing the public keys

- **Decision**: `jwks()` gives `{"keys": [...]}` with the public JWK of every asymmetric key not
  retired, with `kid`, `alg` and `use: sig`. The route is the app's (the guide shows it).
