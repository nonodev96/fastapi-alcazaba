# Security model and resilience

What each tool protects against and what it trusts the backend with, and what happens when the
backend fails.

Every class and function: [the API reference](../reference/integrity.md).

## Security model

What each tool protects against, what it doesn't, and what it needs from your app. Report
vulnerabilities as described in
[SECURITY.md](https://github.com/nonodev96/fastapi-alcazaba/blob/main/SECURITY.md).

### What every tool assumes

- **The backend** is trusted for availability, not for integrity: without a `signing_key` or an
  `encryption_key` on the cache, whoever can write to it decides what your endpoints return,
  forges sessions and runs your tasks. Give the cache one whenever the backend is shared or
  reachable by others; `encryption_key` when entries hold personal data, since signed ones are
  readable in the backend, its dumps and its backups. Keys are SHA-256 digests: arguments, ids
  and tokens never appear in it in clear.
- **What a key doesn't stop:** someone who can write to the backend can still delete entries
  (a revocation, a rate-limit counter) and write back a genuine entry captured earlier: a cached
  response that changed since, a feature flag or maintenance state turned off since. The task
  queue refuses copies of its tasks (each run is claimed once); the other entries don't carry a
  version.
- **Secrets** (the cache's keys, cursor and session secrets, JWT keys, API key peppers) are at
  least 32 bytes, the same in every process, kept in `SecretStr`, and rotate as lists: the first
  signs, all of them verify.
- **The client's address** is the one the ASGI server gives: configure it to trust your proxy,
  and only it. Nothing reads `X-Forwarded-For` itself.
- **Failing:** the security tools fail closed when the backend does: revoked tokens, one-time
  codes, idempotency, webhooks, sessions and the task queue answer 401, 503 or refuse. The cache,
  the rate limiters and the concurrency limiters let requests through by default
  (`fail_open=False` refuses them instead). `InMemoryBackend` never evicts live state: a kind of
  state (sessions, revocations, idempotency records...) that holds `max_size` live entries
  refuses new ones, as Redis with `noeviction` does, so a flood of one kind never makes room by
  forgetting another.

### Per tool

| Tool | Protects against | Doesn't, or needs from you |
| --- | --- | --- |
| Response cache | One user's response served to another: requests are told apart by the endpoint's arguments and the `vary_headers` (`Authorization`, `Cookie`), and per-user entries are sent `private`. Only 200s without `no-store` are kept. | A response that depends on anything else (`request.state`, another header, the client IP) needs it in `vary_headers` or the key builder; with authentication by another header, a shared entry otherwise. |
| Rate and concurrency limits | Floods and brute force from one client: the check and the count are atomic, each route has one budget whatever its path parameters (those of a `Mount` too). | Many clients (a botnet) get a budget each. A limit per account must use the account as the login finds it (normalised), and lets anyone lock the account out for the window: pair it with a limit per IP. |
| Idempotency keys | A retried POST running twice; another client getting a response by guessing a key (keys are per `Authorization`). Replays never carry `Set-Cookie`. | A replay is answered before the route's dependencies: a copy of the request gets the response until it expires, even with a revoked token. An async `identifier` that checks the credentials closes that. |
| JWTs | `alg: none`, a public key used as an HMAC secret, short HMAC secrets, tokens meant for other services (an `aud` without `audience` is refused; a JWKS needs `audience` and `issuer`), revoked tokens within the leeway. | Tokens read from a cookie need `CSRFMiddleware`. |
| Sessions | Fixation (`rotate_session` at login), cookie tossing (`__Host-session`), a copied cookie after logout, sessions left open elsewhere (`UserSessions.end_all` refuses those the index missed too), also from a request still running with it. Ids are 256-bit and stored as digests. | `rotate_session` at login and `clear_session` at logout are yours to call; `CSRFMiddleware` with cookie sessions. |
| OAuth2, OpenID Connect and SAML logins | Login CSRF and code interception (`state` and PKCE in a `__Host-` cookie); ID tokens forged, for another app, from another login or another issuer (signature with the provider's JWKS, `iss`, `aud`, `azp`, `nonce`, `at_hash`; the discovery document's issuer checked); signature wrapping, replayed or unsolicited assertions, assertions for another audience or recipient. | Redirecting after login is the app's: never to a URL from the request. RelayState comes back as the client sent it. |
| Passkeys (WebAuthn) | Phishing (the answer is for the site's RP ID and origin, which the browser enforces), replayed answers (each challenge once), cloned authenticators (the counter), answers for another user or ceremony, a stolen database (public keys only). | Attestation isn't checked (`attestation: "none"`): the authenticator's make isn't known. Synced passkeys are as safe as the account they sync with. |
| Second factor (TOTP) | Codes replayed (each once, in every process), guessed (a lockout after `max_attempts`, recovery codes included), secrets read from the database (sealed with AES-256-GCM for their user), a recovery code used twice, a step-up inherited by another user of the browser. | TOTP doesn't resist a phishing page that relays the code within its 30 seconds: WebAuthn does. The app keeps the user halfway in between the password and the code. |
| Passwords | Hashes cracked from a stolen database (scrypt with OWASP's cost, and a pepper kept outside it), guesses at an account (a lockout after `max_attempts`, in every process), accounts found by timing (unknown ones hash too), leaked passwords (refused, without sending them), reset tokens used twice, guessed or kept after a newer one (256 bits, digests, once). | A lockout lets anyone lock an account out for `lockout` seconds: pair it with a rate limit per IP. Ending the user's sessions after a reset is yours (`UserSessions.end_all`). |
| Revocations, codes, API keys | Revoked tokens accepted while the backend is down (refused unless `fail_open`); brute force of codes (attempts counted atomically, kept past the code); API keys read from the backend (only HMAC digests with a pepper). | |
| Unkey's API keys | Keys deleted, disabled or expired (verified on every request, nothing cached), over their credits or rate limit; Unkey down (a 503, never let through); malformed keys (not sent). | Unkey takes up to 30 s to spread a deletion, and every request waits for its answer: a root key that may only verify for `UnkeyStrategy`, another to make keys. |
| Authorization | Requests the policy doesn't allow, with no way around it through the path: paths with control characters get a 400, HEAD is checked as GET, the subject is the user's `identity`. A broken policy refuses; with Cedar, so does a policy that can't be evaluated, which Cedar alone would skip (a failing `forbid` would let the request through). A policy service that doesn't answer (Open Policy Agent) refuses with a 503, and only a result of `true` allows. | An `anonymous` subject must be a name no user can have (`*anonymous`). In `keyMatch2`, `:name` is a parameter: name objects without colons. Other processes see a policy change when they reload it (a Casbin watcher). |
| CSRF, headers, body size | Cross-site requests sent with the browser's cookies, clickjacking and the like, endless bodies (a 413). | |
| Webhooks and outbound calls | Forged, altered, stale and replayed webhooks; outbound calls to internal addresses (every form of private IP, DNS rebinding, redirects checked again). | |
| Uploads | Files whose name or type lies about what they are, decompression bombs (the canvas of a GIF counts its frames' extents), file names that traverse paths or hide extensions. | Macro-enabled Office documents are told by their content types, read without an XML parser: don't take that as the only check before serving one back. Archives may expand far beyond their size. |
| Object storage | Uploads larger or of another declared type than the form allows (refused by the service), keys that escape their prefix, uploaded HTML or SVG run on the bucket's origin (`filename` downloads it). | The form bounds the declared type, not the file's: check it before using it. A URL works for anyone who has it until it expires: keep `expires` short. |
| E-mail | Headers and recipients added through a field (line breaks, commas, reserved headers refused), mail read on the way (STARTTLS or TLS, certificates checked; plain only to localhost), bodies with links and codes in logs. | SPF, DKIM and DMARC for the sender's domain are your provider's and your DNS's. Escape what goes into the HTML. |
| Pagination | Cursors made up, changed, or used with other filters (signed and bound to the query); limits past the maximum (a 422). | Keyset keys are in plain sight and writable: sort by what clients may see. |
| Task queue | Tasks planted or altered in the backend (sealed, bound to the queue), copies of a sealed task written back (each run is claimed once, until `replay_window`), pickle (arguments travel as JSON). | Delivery is at least once: tasks must be idempotent. Without a key, whoever writes to the backend runs your tasks. |
| Broadcast | Messages planted by whoever can write to Redis (signed with `signing_key`), floods that exhaust memory (64 KiB a message, `queue_size` per subscriber). | Delivery is at most once: a subscriber gets a `gap` for what it missed, and must send the state again. Without a key, a planted message reaches the subscribers. |
| Outbox | A change saved without its message, or a message sent for a change rolled back (both in one transaction). | Delivery is at least once: consumers keep the ids they've seen. Run one relay. |
| Feature flags, maintenance, breakers | Flags and maintenance state planted in the backend (sealed); a deny-listed user getting a flag by not saying who they are (off without a subject). | The subject of a flag is who the request proved to be, never a header. A breaker counts every exception by default: list in `exclude` the errors a client can cause (a 404 for its own input), or anyone can open it. Maintenance's `allow_paths` are prefixes: `/health` also lets `/health/orders` through, which a route `/{tenant}/orders` would serve. |
| Accounting | Going past a quota with concurrent requests (charging is an atomic check-and-add), subjects in clear in the backend (digests), billing that loses charges while the backend is down (the records still have them). | With `meter(..., amount=None)`, the quota is checked when a request starts: the requests running then may together go past it. With `fail_open` (the default), a backend that's down lets requests through unbounded. |
| Audit trail | Events altered, forged, reordered or removed from the middle of a stream (signed and chained). | Removing the newest events of a stream, the oldest ones with everything before them, or a whole stream goes unnoticed: ship events off the host as they're written. |

## Resilience

The cache never makes a request fail. Every backend call has a timeout (`backend_timeout`, 1 s by
default): if it runs out, or the backend fails, the request goes on without cache. After
`circuit_breaker_failures` consecutive failures the backend isn't called at all for
`circuit_breaker_reset` seconds, so that requests don't each wait for the timeout; then one call
tries again.

Concurrent misses for the same key compute once per process (`single_flight`). With several
processes or containers, `distributed_lock=True` makes the first one take a lock in Redis/Valkey
while the others wait for its value (up to `lock_wait` seconds). And `early_refresh=0.1` lets
callers refresh an entry, with growing probability, during the last 10 % of its lifetime: popular
entries are renewed before they expire, instead of every caller missing at once. If that refresh
fails, the cached value is served.
