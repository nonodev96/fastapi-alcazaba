# API reference

Every public class and function, by tool, with its parameters and what it raises. The guide on
the [home page](../index.md) tells how to use them, and the [examples](../examples/index.md) show
them in complete apps.

<div class="grid cards" markdown>

-   :material-cached:{ .lg .middle } __[Response cache](cache.md)__

    ---

    `Cache`, `FastAPICache`, the `@cache` decorator, key builders and coders.

-   :material-database:{ .lg .middle } __[Backends](backends.md)__

    ---

    Where entries and state live: in memory, Redis, Valkey, GLIDE, memcached, DynamoDB, two levels.

-   :material-book-open-page-variant:{ .lg .middle } __[Pagination](pagination.md)__

    ---

    Pages, limit and offset, and signed cursors, with their parameters and responses.

-   :material-speedometer:{ .lg .middle } __[Rate and concurrency limits](limits.md)__

    ---

    Requests per window and requests at a time, per client or shared, across processes.

-   :material-repeat-once:{ .lg .middle } __[Idempotency keys](idempotency.md)__

    ---

    The middleware that replays the first response to a retried request.

-   :material-key-chain:{ .lg .middle } __[API keys, tokens and codes](auth.md)__

    ---

    API keys stored as digests, revoked tokens and one-time codes.

-   :material-shield-lock:{ .lg .middle } __[Sessions, CSRF and security headers](browser.md)__

    ---

    What a cookie-based app needs: server-side sessions, CSRF protection, headers and body limits.

-   :material-clipboard-text-clock:{ .lg .middle } __[Audit trail](audit.md)__

    ---

    `FastAPIAudit`, its entries and events, sinks, and the trail's verification.

-   :material-file-upload:{ .lg .middle } __[Checking uploads](uploads.md)__

    ---

    The type of a file from its bytes, its size and pixels, and safe file names.

-   :material-webhook:{ .lg .middle } __[Webhooks and outbound calls](webhooks.md)__

    ---

    Standard Webhooks in and out, and httpx transports with deadlines, breakers and SSRF checks.

-   :material-chart-timeline-variant:{ .lg .middle } __[Errors and observability](observability.md)__

    ---

    Problem details, request IDs, request deadlines and health checks.

-   :material-calendar-clock:{ .lg .middle } __[Background work](background.md)__

    ---

    Scheduled jobs, the task queue, leader election and locks shared by every process.

-   :material-toggle-switch:{ .lg .middle } __[Operations](operations.md)__

    ---

    Circuit breakers, maintenance mode and feature flags, switched at runtime.

-   :material-shield-check-outline:{ .lg .middle } __[Integrity, resilience and errors](integrity.md)__

    ---

    What signs and encrypts entries, the backends' circuit breaker, and the exceptions.

</div>
