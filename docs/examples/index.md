# Examples

Each example is a small, complete FastAPI app in the repository's
[`examples/`](https://github.com/nonodev96/fastapi-alcazaba/tree/main/examples) folder, written the
way the tools are meant to be used in production, and tested with the library on every change.
They keep their state in memory so that they run without any server; in production, Redis or
Valkey lets every process share it. Only the background work needs one.

They're in the same groups as the [guide](../guide/index.md#next-steps), by what you're
building; each example's page links to the guide's for the same tool.

## Responses and traffic

Faster responses, results a page at a time, fair use, and retries that don't repeat a charge.

<div class="grid cards" markdown>

-   :material-cached:{ .lg .middle } __[Response cache](cache.md)__

    ---

    Shared entries for public pages, private ones per user, invalidation by tag and
    `get_or_set`.

-   :material-book-open-page-variant:{ .lg .middle } __[Pagination](pagination/index.md)__

    ---

    An app per strategy, from page numbers to scrolls and streams; and a catalogue cached three
    ways.

-   :material-speedometer:{ .lg .middle } __[Rate and concurrency limits](limits.md)__

    ---

    Requests per client and per user, shared budgets, and at most N expensive calls at a time.

-   :material-repeat-once:{ .lg .middle } __[Idempotency keys](idempotency.md)__

    ---

    A retried payment gets the first response instead of charging twice.

</div>

## Users and access

Who's calling, how they prove it, and what they may do.

<div class="grid cards" markdown>

-   :material-account-check:{ .lg .middle } __[Authentication strategies](auth_strategies/index.md)__

    ---

    An app per strategy: JWTs, logins with GitHub, sessions, bearer tokens, SAML single sign-on
    and API keys; and three of them on one app.

-   :material-key-chain:{ .lg .middle } __[API keys, tokens and codes](auth.md)__

    ---

    Keys stored as digests with scopes, one-time login codes and revoked tokens.

-   :material-form-textbox-password:{ .lg .middle } __[Passwords](passwords.md)__

    ---

    Sign-up, login and password change, with weak, common and leaked passwords refused and the
    reasons why.

-   :material-cellphone-key:{ .lg .middle } __[Second factor](two_factor.md)__

    ---

    An authenticator app after the password, recovery codes, and a recent code to delete the
    account.

-   :material-fingerprint:{ .lg .middle } __[Passkeys](passkeys.md)__

    ---

    A site without passwords: sign-ups and logins by passkey, and the devices each user is
    logged in on.

-   :material-account-key:{ .lg .middle } __[Authorization](authorization/index.md)__

    ---

    A newsroom whose Casbin policy says who reads, writes, publishes and grants roles, a team's
    documents whose Cedar policies decide by their attributes, and a shop whose refunds Open
    Policy Agent decides, asked over the network.

-   :material-shield-lock:{ .lg .middle } __[A browser app](browser_app.md)__

    ---

    Server-side sessions, CSRF protection and security headers for a cookie-based app.

</div>

## Input data

What clients send, checked before the app keeps it.

<div class="grid cards" markdown>

-   :material-earth:{ .lg .middle } __[Countries and Spain](countries.md)__

    ---

    Customers from anywhere: country codes checked, and a Spanish customer's NIF and province.

-   :material-file-upload:{ .lg .middle } __[Checking uploads](uploads.md)__

    ---

    The real type of a file from its bytes, size and pixel limits, and safe names.

</div>

## Talking to other services

Calls in from other services, and out to theirs.

<div class="grid cards" markdown>

-   :material-webhook:{ .lg .middle } __[Webhooks in and out](webhooks.md)__

    ---

    Signed webhooks received without replays, and sent to user URLs without SSRF.

</div>

## Background and operations

Work outside requests, switches at runtime, and what to look at when something fails.

<div class="grid cards" markdown>

-   :material-calendar-clock:{ .lg .middle } __[Background work](background.md)__

    ---

    Scheduled jobs, a leader among processes and a task queue on Redis streams.

-   :material-toggle-switch:{ .lg .middle } __[Operations](operations.md)__

    ---

    Maintenance mode, feature flags and circuit breakers, switched at runtime.

-   :material-chart-timeline-variant:{ .lg .middle } __[Errors and observability](observability.md)__

    ---

    Problem details, request IDs in logs and responses, deadlines and health checks.

</div>

## Usage and records

What each customer used, and who did what.

<div class="grid cards" markdown>

-   :material-counter:{ .lg .middle } __[Accounting](accounting.md)__

    ---

    Searches and tokens metered per tenant, held to its plan, each charge a record for billing.

-   :material-clipboard-text-clock:{ .lg .middle } __[Audit trail](audit.md)__

    ---

    Who changed or deleted which account, refusals included, signed and chained.

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
