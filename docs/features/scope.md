# Scope and stability

What Alcazaba is for, what it leaves to others, and which of its tools are settled.

## What's in it

Alcazaba gathers the pieces that cut across a FastAPI backend, which most apps end up writing
themselves:

- **State that every process must share,** in Redis, Valkey, memcached or DynamoDB: a cache,
  rate limits and quotas, sessions, revoked tokens, locks, feature flags, a task queue.
- **Security at the edges of a request:** who makes it (authentication, a second factor,
  passkeys), what they may do (authorization), what they used (accounting), what they did (an
  audit trail), and what reaches the app (CSRF, headers, uploads, webhooks, outbound calls).
- **The operations around them:** circuit breakers, maintenance mode, health checks, problem
  details, request IDs, metrics, traces and logs.
- **What leaves the app:** e-mail, files in object storage, messages to its other processes, and
  those a change announces (an outbox).

A tool belongs here when it keeps that kind of state or guards that kind of edge, and is secure by
default: nothing that opens a door stored in clear, a failure that refuses unless the tool says
otherwise, a backend that fails without taking the app down.

## What isn't

| Not in Alcazaba | Instead |
| --- | --- |
| An ORM, migrations, a database layer | The app's: SQLAlchemy, SQLModel… (the [pagination](../guide/pagination/sqlalchemy.md) builds their queries) |
| CORS, compression, static files, trusted hosts | Starlette's middlewares: `CORSMiddleware`, `GZipMiddleware`, `StaticFiles`, `TrustedHostMiddleware` |
| Being an identity provider (issuing tokens to other apps, users and groups, federation, SCIM) | An identity provider; Alcazaba logs in with it ([OIDCStrategy](../guide/authentication/oidc.md), [SAMLStrategy](../guide/authentication/saml.md)) |
| A policy engine of its own | Casbin, Cedar or Open Policy Agent, which [FastAPIAuthz](../guide/authorization/index.md) asks |
| GraphQL, gRPC, server-side rendering | Their libraries; Alcazaba's tools work under any route |
| Billing (invoices, payments, plans) | A billing system, fed by the [usage records](../guide/accounting.md#usage-and-records) |

## Stability

Until 1.0, any release may change any tool: the [release notes](../changelog.md) say what changed,
and what to change in an app. Beyond that, each tool is one of:

- **Stable:** its API has been through releases, apps use it, and it should reach 1.0 as it is.
  A change to it is the exception, said first in the release notes.
- **Experimental:** new, and may still change in any release, as its use shows what's missing or
  wrong. Its guide says so at the top.

| Tool | Stability |
| --- | --- |
| [Response cache](../guide/cache.md), [backends](../guide/backends.md) | Stable |
| [Rate and concurrency limits](../guide/limits.md) | Stable |
| [Idempotency keys](../guide/idempotency.md) | Stable |
| [Authentication strategies](../guide/authentication/index.md): JWT, bearer, session, OAuth2, SAML, scopes | Stable |
| [API keys, tokens and codes](../guide/auth.md) | Stable |
| [Authorization](../guide/authorization/index.md) with [Casbin](../guide/authorization/casbin.md) | Stable |
| [Sessions, CSRF and security headers](../guide/browser.md) | Stable |
| [Audit trail](../guide/audit.md) | Stable |
| [Checking uploads](../guide/uploads.md) | Stable |
| [Webhooks and outbound calls](../guide/webhooks.md) | Stable |
| [Pagination](../guide/pagination/index.md) | Stable |
| [Background work](../guide/background.md), [operations](../guide/operations.md), [errors and observability](../guide/observability.md) | Stable |
| [Cedar](../guide/authorization/cedar.md) and [Open Policy Agent](../guide/authorization/opa.md) | Experimental |
| [OIDCStrategy](../guide/authentication/oidc.md), [UnkeyStrategy](../guide/authentication/unkey.md) | Experimental |
| [Second factor (TOTP)](../guide/two-factor.md), [passkeys (WebAuthn)](../guide/passkeys.md), step-up | Experimental |
| [Each user's sessions](../guide/browser.md#each-users-sessions) (`UserSessions`) | Experimental |
| [Accounting](../guide/accounting.md) | Experimental |
| [Passwords](../guide/passwords.md), [object storage](../guide/storage.md), [e-mail](../guide/mail.md) | Experimental |
| [Broadcast](../guide/background.md#broadcast), [the outbox](../guide/background.md#outbox), [structured logs](../guide/observability.md#structured-logs) | Experimental |

## Towards 1.0

1.0 is when the stable tools stop changing: from then on, a change that breaks an app waits for a
major version, and the release notes of the one before say it's coming. An experimental tool
becomes stable once it has been through releases without changing, and apps use it.
