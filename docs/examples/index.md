# Examples

Each example is a small, complete FastAPI app in the repository's
[`examples/`](https://github.com/nonodev96/fastapi-alcazaba/tree/main/examples) folder, written the
way the tools are meant to be used in production, and tested with the library on every change.
They keep their state in memory so that they run without any server; in production, Redis or
Valkey lets every process share it. Only the background work needs one.

<div class="grid cards" markdown>

-   :material-cached:{ .lg .middle } __[Response cache](cache.md)__

    ---

    Shared entries for public pages, private ones per user, invalidation by tag and
    `get_or_set`.

-   :material-book-open-page-variant:{ .lg .middle } __[Pagination](pagination.md)__

    ---

    Pages, a whole list sliced into pages and signed cursors, each cached its own way.

-   :material-speedometer:{ .lg .middle } __[Rate and concurrency limits](limits.md)__

    ---

    Requests per client and per user, shared budgets, and at most N expensive calls at a time.

-   :material-repeat-once:{ .lg .middle } __[Idempotency keys](idempotency.md)__

    ---

    A retried payment gets the first response instead of charging twice.

-   :material-key-chain:{ .lg .middle } __[API keys, tokens and codes](auth.md)__

    ---

    Keys stored as digests with scopes, one-time login codes and revoked tokens.

-   :material-shield-lock:{ .lg .middle } __[A browser app](browser_app.md)__

    ---

    Server-side sessions, CSRF protection and security headers for a cookie-based app.

-   :material-clipboard-text-clock:{ .lg .middle } __[Audit trail](audit.md)__

    ---

    Who changed or deleted which account, refusals included, signed and chained.

-   :material-file-upload:{ .lg .middle } __[Checking uploads](uploads.md)__

    ---

    The real type of a file from its bytes, size and pixel limits, and safe names.

-   :material-webhook:{ .lg .middle } __[Webhooks in and out](webhooks.md)__

    ---

    Signed webhooks received without replays, and sent to user URLs without SSRF.

-   :material-chart-timeline-variant:{ .lg .middle } __[Errors and observability](observability.md)__

    ---

    Problem details, request IDs in logs and responses, deadlines and health checks.

-   :material-calendar-clock:{ .lg .middle } __[Background work](background.md)__

    ---

    Scheduled jobs, a leader among processes and a task queue on Redis streams.

-   :material-toggle-switch:{ .lg .middle } __[Operations](operations.md)__

    ---

    Maintenance mode, feature flags and circuit breakers, switched at runtime.

</div>

## Running them

From a clone of the repository, each one starts with uvicorn:

```bash
git clone https://github.com/nonodev96/fastapi-alcazaba.git
cd fastapi-alcazaba
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.cache.main:app
```

Then open <http://127.0.0.1:8000/docs> to try its endpoints from the browser. The keys and
secrets they need come from environment variables, with a throwaway value for trying them out:
set real ones in production.
