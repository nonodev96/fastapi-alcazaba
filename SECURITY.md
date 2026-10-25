# Security policy

## Supported versions

Only the latest release of fastapi-alcazaba gets security fixes.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through GitHub's
[security advisories](https://github.com/nonodev96/fastapi-alcazaba/security/advisories/new) for this repository, with:

- the affected version and backend,
- how to reproduce it (a minimal app is ideal),
- the impact you expect.

You'll get an answer within a week. Once a fix is released, the advisory is published with
credit to you, unless you prefer otherwise.

## Scope

In scope: anything that lets a request read another user's cached data, write or poison
entries, run code, or exhaust the server through the cache; and anything that lets a client get
past a rate limiter, or use up someone else's budget, other than by having several IPv4
addresses, IPv6 /64 networks or identities. The same goes for the other tools: getting another
client's response through an idempotency key or a request run twice, a lock held by two
processes while their renewals work, a revoked token accepted or a one-time code guessed beyond
its attempts, a header injected or a body over the limit accepted by the middlewares, errors
leaked by the health checks, an exclusive scheduled job run twice for one tick while the backend
works, and a call let through an open circuit breaker.

Out of scope: problems that need write access to the backend when no `signing_key` or
`encryption_key` is configured, reading the backend when no `encryption_key` is configured (the
README explains when each key is needed), and denial of service of the backend itself.

## Supply chain

Releases are built and published from GitHub Actions with PyPI trusted publishing (no API tokens)
and carry PyPI attestations. The workflows pin every action to a commit and are checked with
[zizmor](https://docs.zizmor.sh/).
