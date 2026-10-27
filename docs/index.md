--8<-- "README.md:header"

--8<-- "README.md:badges"

Alcazaba (`fastapi-alcazaba`) gathers the pieces most FastAPI apps end up writing themselves,
built with security in mind and tested against real servers. The guide has a page per tool, the
examples show them at work in complete apps, and the API reference lists every class and
function.

[Get started](guide/index.md){ .md-button .md-button--primary }
[Examples](examples/index.md){ .md-button }
[API reference](reference/index.md){ .md-button }

## The tools

<div class="grid cards" markdown>

-   :material-cached:{ .lg .middle } __[Response cache](guide/cache.md)__

    ---

    `@cache` for endpoints and functions: one entry for everybody or one per user, invalidation
    by tag, signed or encrypted entries.

-   :material-database:{ .lg .middle } __[Backends](guide/backends.md)__

    ---

    In memory, Redis, Valkey, GLIDE, memcached or DynamoDB, and a copy in each process in front
    of them.

-   :material-book-open-page-variant:{ .lg .middle } __[Pagination](guide/pagination.md)__

    ---

    Pages, limit and offset, and signed cursors, each page cached under its own key.

-   :material-speedometer:{ .lg .middle } __[Rate and concurrency limits](guide/limits.md)__

    ---

    Requests per window and requests at a time, per client or shared, across processes.

-   :material-repeat-once:{ .lg .middle } __[Idempotency keys](guide/idempotency.md)__

    ---

    A retried POST gets the first response instead of running twice.

-   :material-key-chain:{ .lg .middle } __[API keys, tokens and codes](guide/auth.md)__

    ---

    API keys with scopes and expiry, a blocklist of tokens and one-time codes, nothing stored in
    clear.

-   :material-shield-lock:{ .lg .middle } __[Sessions, CSRF and security headers](guide/browser.md)__

    ---

    Sessions you can revoke, requests from other sites refused, the OWASP headers and a body size
    limit.

-   :material-clipboard-text-clock:{ .lg .middle } __[Audit trail](guide/audit.md)__

    ---

    Who did what to which resource, and whether it worked: no secrets, signed and chained.

-   :material-file-upload:{ .lg .middle } __[Checking uploads](guide/uploads.md)__

    ---

    The real type from the bytes, the size, image dimensions and safe file names.

-   :material-webhook:{ .lg .middle } __[Webhooks and outbound calls](guide/webhooks.md)__

    ---

    Standard Webhooks signing and verification, and calls out with a deadline, a breaker and SSRF
    protection.

-   :material-chart-timeline-variant:{ .lg .middle } __[Errors and observability](guide/observability.md)__

    ---

    RFC 9457 problem details, request IDs, timeouts, health checks, metrics and traces.

-   :material-calendar-clock:{ .lg .middle } __[Background work](guide/background.md)__

    ---

    Scheduled jobs, a task queue, a leader and locks, shared by every process.

-   :material-toggle-switch:{ .lg .middle } __[Operations](guide/operations.md)__

    ---

    Circuit breakers, maintenance mode and feature flags that every process sees at once.

-   :material-shield-check-outline:{ .lg .middle } __[Security model and resilience](guide/security-model.md)__

    ---

    What gets cached and for whom, what the backend is trusted with, and what happens when it
    fails.

</div>

## Safe by default

--8<-- "README.md:safe-by-default"

## License and credits

--8<-- "README.md:license-and-credits"
