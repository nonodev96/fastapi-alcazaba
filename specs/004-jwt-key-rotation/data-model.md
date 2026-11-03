# Data Model: JWT keys

## JWTKey

| Field | Meaning |
| --- | --- |
| `kid` | given, or derived (R2) |
| `algorithm` | the only algorithm it signs and verifies with |
| `until` | aware datetime past which it verifies nothing; None for no end |
| `can_sign` | an HMAC secret or a private key |
| (private) signing key | the secret, or the private key |
| (private) verifying key | the secret, or the public key (derived from a private one) |

## Strategy keys (local)

Ordered; the first signs and has no `until`; `kid`s unique. Entries made from `key=` accept every
algorithm of `algorithms` and count as keys whose `kid` wasn't given.

## Remote key set (JWKS)

| Field | Meaning |
| --- | --- |
| keys | JWK dicts by `kid` (None for a key without one), usable ones only |
| fetched | monotonic time of the last successful fetch |
| attempted | monotonic time of the last fetch, successful or not |
| parsed | `PyJWK` by (`kid`, algorithm), built on first use |

A key is usable until `fetched + jwks_lifespan`; while fetches fail, until
`fetched + jwks_lifespan + jwks_stale`.
