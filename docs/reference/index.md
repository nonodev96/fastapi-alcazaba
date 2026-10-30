# Reference

Here's the reference, or code API: every public class and function, by tool, with its parameters
and what it raises. The [Tutorial - User Guide](../guide/index.md) tells how to use them, and the
[examples](../examples/index.md) show them in complete apps.

<div class="grid cards" markdown>

-   :material-cached:{ .lg .middle } __[Response cache](cache.md)__

    ---

    `Cache`, `FastAPICache`, the `@cache` decorator, key builders and coders.
    In `fastapi_alcazaba.response_cache`.

-   :material-database:{ .lg .middle } __[Backends](backends.md)__

    ---

    Where entries and state live: in memory, Redis, Valkey, GLIDE, memcached, DynamoDB, two levels.
    In `fastapi_alcazaba.backends`.

-   :material-book-open-page-variant:{ .lg .middle } __[Pagination](pagination.md)__

    ---

    The twelve strategies, with their parameters and pages. In `fastapi_alcazaba.pagination`.

-   :material-speedometer:{ .lg .middle } __[Rate and concurrency limits](limits.md)__

    ---

    Requests per window and requests at a time, per client or shared, across processes.
    In `fastapi_alcazaba.rate_limit`.

-   :material-repeat-once:{ .lg .middle } __[Idempotency keys](idempotency.md)__

    ---

    The middleware that replays the first response to a retried request.
    In `fastapi_alcazaba.idempotency`.

-   :material-account-check:{ .lg .middle } __[Authentication strategies](authentication.md)__

    ---

    `FastAPIAuth`, `Strategy`, and the JWT, bearer, OAuth2, SAML and session strategies.
    In `fastapi_alcazaba.auth`.

-   :material-key-chain:{ .lg .middle } __[API keys, tokens and codes](auth.md)__

    ---

    API keys stored as digests, revoked tokens and one-time codes.
    In `fastapi_alcazaba.auth`.

-   :material-account-key:{ .lg .middle } __[Authorization with Casbin](authz.md)__

    ---

    A Casbin policy per route or for every request: `FastAPICasbin` and `CasbinMiddleware`.
    In `fastapi_alcazaba.authz`.

-   :material-shield-lock:{ .lg .middle } __[Sessions, CSRF and security headers](browser.md)__

    ---

    What a cookie-based app needs: server-side sessions, CSRF protection, headers and body limits.
    In `fastapi_alcazaba.security`.

-   :material-clipboard-text-clock:{ .lg .middle } __[Audit trail](audit.md)__

    ---

    `FastAPIAudit`, its entries and events, sinks, and the trail's verification.
    In `fastapi_alcazaba.audit`.

-   :material-file-upload:{ .lg .middle } __[Checking uploads](uploads.md)__

    ---

    The type of a file from its bytes, its size and pixels, and safe file names.
    In `fastapi_alcazaba.uploads`.

-   :material-webhook:{ .lg .middle } __[Webhooks and outbound calls](webhooks.md)__

    ---

    Standard Webhooks in and out, and httpx transports with deadlines, breakers and SSRF checks.
    In `fastapi_alcazaba.webhooks` and `fastapi_alcazaba.outbound`.

-   :material-chart-timeline-variant:{ .lg .middle } __[Errors and observability](observability.md)__

    ---

    Problem details, request IDs, request deadlines and health checks.
    In `fastapi_alcazaba.observability`.

-   :material-calendar-clock:{ .lg .middle } __[Background work](background.md)__

    ---

    Scheduled jobs, the task queue, leader election and locks shared by every process.
    In `fastapi_alcazaba.background`.

-   :material-toggle-switch:{ .lg .middle } __[Operations](operations.md)__

    ---

    Circuit breakers, maintenance mode and feature flags, switched at runtime.
    In `fastapi_alcazaba.operations`.

-   :material-shield-check-outline:{ .lg .middle } __[Integrity, resilience and errors](integrity.md)__

    ---

    What signs and encrypts entries, RFC 8785 canonical JSON, the backends' circuit breaker, and
    the exceptions.
    In `fastapi_alcazaba.core`.

</div>
