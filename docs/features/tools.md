# The tools

Each tool has a page in the [Tutorial - User Guide](../guide/index.md), an
[example app](../examples/index.md) and its [reference](../reference/index.md).

<div class="grid cards" markdown>

-   :material-cached:{ .lg .middle } __[Response cache](../guide/cache.md)__

    ---

    `@cache` for endpoints and functions: one entry for everybody or one per user, invalidation
    by tag, signed or encrypted entries.

-   :material-database:{ .lg .middle } __[Backends](../guide/backends.md)__

    ---

    In memory, Redis, Valkey, GLIDE, memcached or DynamoDB, and a copy in each process in front
    of them.

-   :material-book-open-page-variant:{ .lg .middle } __[Pagination](../guide/pagination/index.md)__

    ---

    Seven strategies: page numbers, offsets, ranges, signed cursors, keysets, scrolls and
    streams, each page cached under its own key.

-   :material-speedometer:{ .lg .middle } __[Rate and concurrency limits](../guide/limits.md)__

    ---

    Requests per window and requests at a time, per client or shared, across processes.

-   :material-repeat-once:{ .lg .middle } __[Idempotency keys](../guide/idempotency.md)__

    ---

    A retried POST gets the first response instead of running twice.

-   :material-account-check:{ .lg .middle } __[Authentication strategies](../guide/authentication/index.md)__

    ---

    Who makes a request, by strategies: JWTs, bearer tokens, sessions, OAuth2 logins with GitHub
    or Google, SAML single sign-on, or your own.

-   :material-key-chain:{ .lg .middle } __[API keys, tokens and codes](../guide/auth.md)__

    ---

    API keys with scopes and expiry, a blocklist of tokens and one-time codes, nothing stored in
    clear.

-   :material-form-textbox-password:{ .lg .middle } __[Passwords](../guide/passwords.md)__

    ---

    Slow hashes with a pepper, leaked passwords refused, guesses limited per account, and resets
    by a link that works once.

-   :material-fingerprint:{ .lg .middle } __[Passkeys](../guide/passkeys.md)__

    ---

    Logins without a password with Touch ID, Windows Hello, a phone or a security key (WebAuthn),
    which a phishing page can't relay.

-   :material-cellphone-key:{ .lg .middle } __[Second factor](../guide/two-factor.md)__

    ---

    Authenticator apps' codes (TOTP) after the password, each once, recovery codes, and a recent
    code for sensitive actions.

-   :material-account-key:{ .lg .middle } __[Authorization](../guide/authorization/index.md)__

    ---

    Who may do what, decided by a Casbin or Cedar policy in the process, or by Open Policy Agent
    over the network (ACL, RBAC, ABAC), per route or for every request.

-   :material-shield-lock:{ .lg .middle } __[Sessions, CSRF and security headers](../guide/browser.md)__

    ---

    Sessions you can revoke, requests from other sites refused, the OWASP headers and a body size
    limit.

-   :material-counter:{ .lg .middle } __[Accounting](../guide/accounting.md)__

    ---

    What each user, API key or tenant uses per period, held to its plan's quotas, with a usage
    record per charge for billing.

-   :material-cash-lock:{ .lg .middle } __[Provider budgets](../guide/provider-budget.md)__

    ---

    What each user may cost the app at an LLM, SMS or OCR provider: reserved before each call,
    with limits an admin panel changes in the database.

-   :material-clipboard-text-clock:{ .lg .middle } __[Audit trail](../guide/audit.md)__

    ---

    Who did what to which resource, and whether it worked: no secrets, signed and chained.

-   :material-file-upload:{ .lg .middle } __[Checking uploads](../guide/uploads.md)__

    ---

    The real type from the bytes, the size, image dimensions and safe file names.

-   :material-bucket:{ .lg .middle } __[Object storage](../guide/storage.md)__

    ---

    Signed URLs and upload forms for S3, R2 or MinIO: files go straight from the browser, bounded
    in size and type.

-   :material-email-fast:{ .lg .middle } __[E-mail](../guide/mail.md)__

    ---

    Password resets, codes and receipts with their invoice, over SMTP with TLS or Amazon SES's
    API, in messages nothing can add a header or a recipient to.

-   :material-webhook:{ .lg .middle } __[Webhooks and outbound calls](../guide/webhooks.md)__

    ---

    Standard Webhooks signing and verification, and calls out with a deadline, a breaker and SSRF
    protection.

-   :material-chart-timeline-variant:{ .lg .middle } __[Errors and observability](../guide/observability.md)__

    ---

    RFC 9457 problem details, request IDs, timeouts, health checks, metrics, traces and logs as
    JSON.

-   :material-calendar-clock:{ .lg .middle } __[Background work](../guide/background.md)__

    ---

    Scheduled jobs, a task queue, a leader and locks, shared by every process; messages to all of
    them, and an outbox for those a change announces.

-   :material-toggle-switch:{ .lg .middle } __[Operations](../guide/operations.md)__

    ---

    Circuit breakers, maintenance mode and feature flags that every process sees at once.

-   :material-shield-check-outline:{ .lg .middle } __[Security model and resilience](../guide/security-model.md)__

    ---

    What gets cached and for whom, what the backend is trusted with, and what happens when it
    fails.

</div>
