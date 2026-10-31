"""The animations at the top of the documentation's examples: what each tool is for, as a sequence
diagram whose steps appear one by one (docs/stylesheets/animations.css moves them).

    python scripts/docs_animations.py

writes docs/examples/animations/<name>.svg, which the example pages take in with
`--8<-- "docs/examples/animations/<name>.svg"`. Each one is an inline SVG figure drawn in the
theme's colours, so it follows the light and dark schemes. A label too long for the room between
its lanes, or a note wider than the picture, is an error rather than an overlap.
"""

import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path

OUT = Path(__file__).parents[1] / "docs" / "examples" / "animations"
WIDTH = 640
TOP = 92  # the y of the first step
ROW = 36  # between steps
CHAR = 7.6  # the width of a character of the labels (12.5px, monospace)
MAX_STEPS = 8  # what animations.css times: alc-s1...alc-s9 (the banner) and alc-m1...alc-m8
LANES = {2: (150, 490), 3: (100, 320, 540), 4: (80, 240, 400, 560)}


@dataclass(frozen=True)
class Arrow:
    """A message from one lane to another: a request, a reply, or a refusal."""

    source: int
    target: int
    label: str
    kind: str = "request"


@dataclass(frozen=True)
class Note:
    """What happens inside one lane: a check, a decision."""

    lane: int
    label: str
    refused: bool = False


@dataclass(frozen=True)
class Animation:
    title: str
    purpose: str
    actors: tuple[str, ...]
    steps: tuple[Arrow | Note, ...]
    banner: str


def request(source: int, target: int, label: str) -> Arrow:
    return Arrow(source, target, label)


def reply(source: int, target: int, label: str) -> Arrow:
    return Arrow(source, target, label, "reply")


def refused(source: int, target: int, label: str) -> Arrow:
    return Arrow(source, target, label, "refused")


ANIMATIONS: dict[str, Animation] = {
    "cache": Animation(
        "Response cache",
        "What it's for: an endpoint computed once and served from the cache, to everybody or to"
        " each user, until a change drops it.",
        ("Client", "Your app", "Cache"),
        (
            request(0, 1, "GET /products/1"),
            request(1, 2, "miss: run it, store it"),
            reply(1, 0, "200, miss; stored"),
            request(0, 1, "GET /products/1"),
            reply(2, 1, "hit: the stored response"),
            reply(1, 0, "200, hit; ttl=600"),
            request(0, 1, "PUT /products/1"),
            request(1, 2, "drop tag product:1"),
        ),
        "Computed once, served from the cache until it changes",
    ),
    "pagination": Animation(
        "Pagination",
        "What it's for: lists served a page at a time, by page number or by cursor, with limits"
        " that clients can't go past, and each page cached apart.",
        ("Client", "Your app", "Cache"),
        (
            request(0, 1, "GET ?page=2&page_size=3"),
            request(1, 2, "page 2: its own entry"),
            reply(1, 0, "200, 3 items, Link: next"),
            request(0, 1, "GET ?page_size=1000"),
            refused(1, 0, "422, page_size ≤ 100"),
            request(0, 1, "GET …/newest?cursor=NTk…"),
            reply(1, 0, "200, the 2 after it"),
        ),
        "Pages checked, linked, and cached each under its own key",
    ),
    "pagination_page_number": Animation(
        "PaginationPageNumber",
        "What it's for: numbered pages that people browse and jump through, with the total and"
        " the number of pages, each page cached under its own key.",
        ("Reader", "Your app", "Database"),
        (
            request(0, 1, "GET ?page=2&page_size=3"),
            request(1, 2, "count, OFFSET 3 LIMIT 3"),
            reply(2, 1, "45 posts; 3 rows"),
            reply(1, 0, "200, page 2 of 15"),
            Note(1, "Link: first, prev, next, last"),
            request(0, 1, "GET ?page=2&page_size=3"),
            reply(1, 0, "200, from the cache"),
        ),
        "Numbered pages, with the total, each page cached",
    ),
    "pagination_cursor": Animation(
        "PaginationCursor",
        "What it's for: feeds and infinite scroll, whose pages don't shift when items arrive,"
        " with signed cursors both ways that clients can't make up nor reuse with other filters.",
        ("Reader", "Your app", "Database"),
        (
            request(0, 1, "GET /events?limit=3"),
            request(1, 2, "newest, LIMIT 4"),
            reply(1, 0, "200, 30 29 28, next_cursor"),
            Note(2, "event 31 arrives"),
            request(0, 1, "GET ?cursor=<next>"),
            reply(1, 0, "200, 27 26 25, both cursors"),
            request(0, 1, "the cursor, changed"),
            refused(1, 0, "400, invalid cursor"),
        ),
        "Pages that don't shift, both ways, behind a signature",
    ),
    "pagination_offset": Animation(
        "PaginationOffset",
        "What it's for: slices of a list too big, or too busy, to count, each saying whether"
        " there's another after it.",
        ("Client", "Your app", "Database"),
        (
            request(0, 1, "GET /logs?offset=0&limit=3"),
            request(1, 2, "LIMIT 4 OFFSET 0"),
            reply(2, 1, "4 rows: one more"),
            reply(1, 0, "200, 3 items, has_more"),
            request(0, 1, "GET /logs?offset=3&limit=3"),
            reply(1, 0, "200, the next 3"),
            request(0, 1, "GET /logs?limit=500"),
            refused(1, 0, "422, limit ≤ 100"),
        ),
        "One row more tells if there's a next page: nothing counted",
    ),
    "pagination_range": Animation(
        "PaginationRange",
        "What it's for: data grids that ask for the rows they show by index, and read which ones"
        " they got, and how many there are, from Content-Range.",
        ("Client", "Your app", "Database"),
        (
            request(0, 1, "GET …?start=0&end=24"),
            request(1, 2, "count, LIMIT 25 OFFSET 0"),
            reply(1, 0, "200, 25 items"),
            Note(1, "Content-Range: items 0-24/319"),
            request(0, 1, "GET …?start=319"),
            refused(1, 0, "416, items */319"),
            request(0, 1, "GET …?start=10&end=5"),
            refused(1, 0, "400, end before start"),
        ),
        "The rows by index, and the total in Content-Range",
    ),
    "pagination_keyset": Animation(
        "PaginationKeyset",
        "What it's for: clients that keep the key of the last item they have, and ask for what"
        " came after it: syncing, chats, exports.",
        ("Client", "Your app", "Database"),
        (
            request(0, 1, "GET /messages?after=58"),
            request(1, 2, "WHERE id > 58 LIMIT 21"),
            reply(1, 0, "200, 59 60, after: 60"),
            Note(2, "message 61 is written"),
            request(0, 1, "GET /messages?after=60"),
            reply(1, 0, "200, 61 alone"),
            request(0, 1, "GET /messages?after=abc"),
            refused(1, 0, "400, invalid after"),
        ),
        "The key of the last item seen: rows written meanwhile shift nothing",
    ),
    "pagination_scroll": Animation(
        "PaginationScroll",
        "What it's for: a result set read in batches as it was when the scroll started, whatever"
        " is written meanwhile: exports and long jobs.",
        ("Client", "Your app", "Backend"),
        (
            request(0, 1, "GET /export?batch_size=4"),
            request(1, 2, "keep the 12 found, 60 s"),
            reply(1, 0, "200, 4 items, scroll_id"),
            Note(1, "order 13 placed: not in it"),
            request(0, 1, "GET …?scroll_id=WyJs…"),
            reply(2, 1, "orders 5 to 8, kept 60 s"),
            reply(1, 0, "200, the same if retried"),
            refused(1, 0, "404 once it has expired"),
        ),
        "One query, kept as a snapshot and read batch by batch",
    ),
    "pagination_streaming": Animation(
        "PaginationStreaming",
        "What it's for: every item in one response, as they come, without pages, read from a big"
        " table a batch at a time by key.",
        ("Client", "Your app", "Database"),
        (
            request(0, 1, "GET /events"),
            request(1, 2, "ORDER BY id LIMIT 100"),
            reply(1, 0, "200, 100 lines of NDJSON"),
            request(1, 2, "id > 100 LIMIT 100"),
            reply(1, 0, "100 more lines"),
            request(1, 2, "id > 200 LIMIT 100"),
            reply(2, 1, "50 rows: the last batch"),
            reply(1, 0, "50 lines, end"),
        ),
        "Every item in one response, read and sent a batch at a time",
    ),
    "limits": Animation(
        "Rate and concurrency limits",
        "What it's for: how many requests each client may make in a window, and how many may run"
        " at once, counted across every process.",
        ("Client", "Your app", "Counters"),
        (
            request(0, 1, "GET /search?q=oli"),
            request(1, 2, "+1 for this address"),
            reply(1, 0, "200, RateLimit: r=9"),
            Note(0, "… 9 more in a minute"),
            request(0, 1, "GET /search?q=oli"),
            reply(2, 1, "no budget left"),
            refused(1, 0, "429, Retry-After: 22"),
        ),
        "Each client its budget, the same in every process",
    ),
    "idempotency": Animation(
        "Idempotency keys",
        "What it's for: a client that retries after a timeout or a dropped connection gets the"
        " first response again, instead of doing it twice.",
        ("Client", "Your app", "Cache"),
        (
            request(0, 1, "POST /payments, key k1"),
            Note(1, "charges the card"),
            request(1, 2, "keep the 201 under k1"),
            refused(1, 0, "201, lost: timeout"),
            request(0, 1, "retry, the same key k1"),
            reply(2, 1, "the kept 201"),
            reply(1, 0, "201, Idempotent-Replayed"),
        ),
        "A retry gets the first response: the card is charged once",
    ),
    "auth": Animation(
        "API keys, tokens and codes",
        "What it's for: people sign in with a one-time code and get a revocable token; their"
        " programs call the API with keys that are stored only as digests.",
        ("Owner", "Your app", "Program"),
        (
            request(0, 1, "POST /login/code, e-mail"),
            reply(1, 0, "a code, by e-mail"),
            request(0, 1, "POST /login, the code"),
            reply(1, 0, "an access token"),
            request(0, 1, "POST /api-keys, token"),
            reply(1, 0, "acme_…, shown once"),
            request(2, 1, "GET /orders, X-API-Key"),
            reply(1, 2, "200, its owner's orders"),
        ),
        "Codes for people, keys for programs, only digests stored",
    ),
    "authz": Animation(
        "Authorization with Casbin",
        "What it's for: a Casbin policy (roles and their permissions) decides who may do what,"
        " for each route or each action, instead of checks spread over the code.",
        ("User", "Your app", "Casbin policy"),
        (
            request(0, 1, "cris: POST …/3/publish"),
            request(1, 2, "(cris, articles, publish)?"),
            reply(2, 1, "deny: a writer"),
            refused(1, 0, "403 Forbidden"),
            request(0, 1, "ben: POST …/3/publish"),
            request(1, 2, "(ben, articles, publish)?"),
            reply(2, 1, "allow: an editor"),
            reply(1, 0, "200, published by ben"),
        ),
        "Who may do what is in the policy, not in the routes",
    ),
    "browser_app": Animation(
        "A browser app",
        "What it's for: a server-rendered app whose sessions live on the server, whose forms only"
        " its own pages can send, and whose pages carry the security headers.",
        ("Browser", "Your app", "Sessions"),
        (
            request(0, 1, "GET /"),
            reply(1, 0, "page, CSRF token, CSP"),
            request(0, 1, "POST /cart, X-CSRF-Token"),
            request(1, 2, "the cart, in the session"),
            reply(1, 0, "303 → /"),
            request(0, 1, "another site's POST /logout"),
            refused(1, 0, "403, cross-site"),
        ),
        "Sessions on the server; forms only from your own pages",
    ),
    "audit": Animation(
        "Audit trail",
        "What it's for: a record of who did what to which resource, allowed or not, that nobody"
        " can alter without it showing.",
        ("Admin", "Your app", "Audit trail"),
        (
            request(0, 1, "PUT /accounts/u3/role"),
            request(1, 2, "account.role.change ✓"),
            reply(1, 0, "200"),
            request(0, 1, "DELETE /accounts/u3, sam"),
            request(1, 2, "account.delete, denied"),
            refused(1, 0, "403 Forbidden"),
            request(0, 1, "GET /audit/verify"),
            reply(1, 0, "no event altered"),
        ),
        "Who did what, when and from where: signed and chained",
    ),
    "uploads": Animation(
        "Checking uploads",
        "What it's for: files judged by what they are, not by the name or type the client gives,"
        " before the app stores them or serves them back.",
        ("User", "Your app", "Storage"),
        (
            request(0, 1, "POST /avatars, cat.png"),
            Note(1, "✓ a PNG, by its bytes"),
            request(1, 2, "stored as <uuid>.png"),
            reply(1, 0, "201 Created"),
            request(0, 1, "cat.html, named cat.png"),
            Note(1, "✗ HTML, whatever its name", refused=True),
            refused(1, 0, "415 Unsupported Media Type"),
        ),
        "Judged by their bytes, stored under names of the app's own",
    ),
    "webhooks": Animation(
        "Webhooks in and out",
        "What it's for: webhooks taken only when signed with the sender's secret, each delivery"
        " once, and webhooks sent signed, to URLs that can't reach the app's own network.",
        ("Payment provider", "Your shop", "User's endpoint"),
        (
            request(0, 1, "POST /webhooks/payments"),
            Note(1, "✓ signature, fresh, new"),
            reply(1, 0, "204"),
            request(0, 1, "the same delivery again"),
            refused(1, 0, "401, seen already"),
            request(1, 2, "order.paid, signed"),
            reply(2, 1, "200"),
        ),
        "Signed webhooks in, each once; signed webhooks out",
    ),
    "observability": Animation(
        "Errors and observability",
        "What it's for: errors that clients can act on without learning anything about the"
        " server, a request ID that ties a complaint to its log lines, and a deadline for every"
        " request.",
        ("Client", "Your app", "Logs"),
        (
            request(0, 1, "POST /orders, invalid"),
            refused(1, 0, "422, problem details"),
            request(0, 1, "GET /orders/3/invoice"),
            request(1, 2, "traceback, request_id"),
            refused(1, 0, "500, nothing about it"),
            request(0, 1, "GET /reports/sales"),
            refused(1, 0, "504 at the deadline"),
        ),
        "Clear errors, nothing leaked, one ID in every log line",
    ),
    "background": Animation(
        "Background work",
        "What it's for: work that goes on after the response (e-mails, reports, relays), run in"
        " one of the processes, retried while it fails, and kept when it can't be done.",
        ("Client", "Your app", "Worker"),
        (
            request(0, 1, "POST /users"),
            request(1, 2, "queue: welcome e-mail"),
            reply(1, 0, "201, at once"),
            Note(2, "mailer down: retries", refused=True),
            Note(2, "then a dead letter", refused=True),
            request(0, 1, "admin: requeue it"),
            request(1, 2, "the task, again"),
            Note(2, "✓ e-mail sent"),
        ),
        "Work after the response: retried, kept, never lost",
    ),
    "operations": Animation(
        "Operations",
        "What it's for: switches flipped at runtime, without a deploy: maintenance mode, features"
        " rolled out to some of the users, and a breaker for a service that's down.",
        ("Users", "Your app", "Admin"),
        (
            request(2, 1, "PUT /admin/maintenance"),
            request(0, 1, "GET /checkout"),
            refused(1, 0, "503, Retry-After: 900"),
            request(2, 1, "DELETE /admin/maintenance"),
            request(2, 1, "new-checkout: 30 %"),
            request(0, 1, "GET /checkout"),
            Note(1, "is the user in the 30 %?"),
            reply(1, 0, "200, new or classic"),
        ),
        "Close, reopen and roll out at runtime, without a deploy",
    ),
    "auth_strategies": Animation(
        "One route, several strategies",
        "What it's for: one route that browsers reach with their session and scripts with a"
        " token, each strategy tried in order.",
        ("Browser", "Your app", "Script"),
        (
            request(0, 1, "GET /me, Cookie: session"),
            Note(1, '"session" ✓'),
            reply(1, 0, "200 octocat"),
            request(2, 1, "GET /me, Bearer eyJ…"),
            Note(1, '"session" ✗ → "jwt" ✓'),
            reply(1, 2, "200 octocat"),
        ),
        'auth.authenticate("session", "jwt"): the first that finds the user',
    ),
    "auth_jwt": Animation(
        "JWTStrategy",
        "What it's for: scripts and other services prove who they are with a signed token, which"
        " the app checks without looking anything up.",
        ("Script", "Your app", "Revoked tokens"),
        (
            request(0, 1, "POST /token, password"),
            reply(1, 0, "a signed JWT, for 1 h"),
            request(0, 1, "GET /me, Bearer eyJ…"),
            Note(1, "✓ signature, exp, iss, aud"),
            request(1, 2, "jti revoked?"),
            reply(2, 1, "no"),
            reply(1, 0, '200 {"login": "octocat"}'),
        ),
        "Who sends it is in the token, signed: no session to look up",
    ),
    "auth_oauth2": Animation(
        "OAuth2Strategy",
        "What it's for: people log in with an account they already have (GitHub, Google…), and"
        " your app never sees their password.",
        ("Browser", "Your app", "GitHub"),
        (
            request(0, 1, "GET /login/github"),
            reply(1, 0, "302 → GitHub, state, PKCE"),
            request(0, 2, "log in at GitHub, allow the app"),
            reply(2, 0, "302 → callback ?code=…&state=…"),
            request(0, 1, "GET /auth/github/callback"),
            request(1, 2, "code + verifier → profile"),
            reply(1, 0, "session cookie, 303 /me"),
        ),
        "Logged in with GitHub: your app never sees the password",
    ),
    "auth_session": Animation(
        "SessionStrategy",
        "What it's for: a browser stays logged in after a login, holding only a random id; who"
        " the user is stays on the server.",
        ("Browser", "Your app", "Sessions"),
        (
            request(0, 1, "POST /login, password"),
            request(1, 2, "new session: user 1"),
            reply(1, 0, "Set-Cookie: session=<id>"),
            request(0, 1, "GET /me, Cookie: session"),
            request(1, 2, "load session <id>"),
            reply(2, 1, "user 1"),
            reply(1, 0, '200 {"login": "octocat"}'),
        ),
        "The browser holds an id; the user stays on the server",
    ),
    "auth_bearer": Animation(
        "HTTPBearerStrategy",
        "What it's for: tokens your app creates and looks up, such as personal access tokens,"
        " which stop working the moment they're deleted.",
        ("Script", "Your app", "Database"),
        (
            request(0, 1, "GET /me, Bearer pat_…"),
            request(1, 2, "SHA-256(token) → user?"),
            reply(2, 1, "user 1"),
            reply(1, 0, '200 {"login": "octocat"}'),
            request(0, 1, "DELETE /tokens/current"),
            request(1, 2, "delete the digest"),
            request(0, 1, "GET /me, Bearer pat_…"),
            refused(1, 0, "401: refused at once"),
        ),
        "The token says nothing by itself: delete its row, and it's dead",
    ),
    "auth_saml": Animation(
        "SAMLStrategy",
        "What it's for: single sign-on with the company's identity provider: one login there"
        " opens every app.",
        ("Browser", "Your app", "Identity provider"),
        (
            request(0, 1, "GET /saml/login"),
            reply(1, 0, "302 → IdP, SAML request"),
            request(0, 2, "log in at the IdP, once for every app"),
            reply(2, 0, "a signed assertion, in a form"),
            request(0, 1, "POST /saml/acs, assertion"),
            Note(1, "✓ signature, audience, request, once"),
            reply(1, 0, "session cookie, 303 /me"),
        ),
        "One login at the identity provider opens every app",
    ),
    "auth_your_own": Animation(
        "Your own strategy",
        "What it's for: any other credential (an API key, a client certificate, a signed"
        " request): you write authenticate(), FastAPIAuth does the rest.",
        ("Program", "Your app", "API keys"),
        (
            request(0, 1, "GET /me, X-API-Key: demo_…"),
            Note(1, "APIKeyStrategy.authenticate()"),
            request(1, 2, "key id → digest, owner"),
            reply(2, 1, "owner: user 1"),
            reply(1, 0, '200 {"login": "octocat"}'),
            request(0, 1, "GET /me, a wrong key"),
            refused(1, 0, "401, APIKey challenge"),
        ),
        "Return the user, None, or raise AuthenticationFailed",
    ),
}


def render(name: str, animation: Animation) -> str:
    """The figure: the SVG and its caption. Raises ValueError for what wouldn't fit."""
    if len(animation.steps) > MAX_STEPS:
        raise ValueError(f"{name}: at most {MAX_STEPS} steps, animations.css times no more")
    xs = LANES[len(animation.actors)]
    box = min(164, xs[1] - xs[0] - 12)
    banner_y = TOP + (len(animation.steps) - 1) * ROW + 30
    lines = [
        '<figure class="alc-anim">',
        f'<svg viewBox="0 0 {WIDTH} {banner_y + 44}" role="img"'
        f' aria-labelledby="alc-{name}-title alc-{name}-desc" xmlns="http://www.w3.org/2000/svg">',
        f'<title id="alc-{name}-title">{escape(animation.title)}</title>',
        f'<desc id="alc-{name}-desc">{escape(describe(animation))}</desc>',
    ]
    for x, actor in zip(xs, animation.actors, strict=True):
        lines.append(f'<line class="alc-life" x1="{x}" y1="52" x2="{x}" y2="{banner_y - 8}"/>')
        lines.append(
            f'<g class="alc-actor"><rect x="{x - box // 2}" y="10" width="{box}" height="42"'
            f' rx="8"/><text x="{x}" y="36">{escape(actor)}</text></g>'
        )
    for index, step in enumerate(animation.steps, start=1):
        y = TOP + (index - 1) * ROW
        if isinstance(step, Note):
            lines.append(note(name, step, xs[step.lane], y, index))
        else:
            lines.extend(arrow(name, step, xs[step.source], xs[step.target], y, index))
    lines += [
        f'<g class="alc-step alc-banner alc-s{len(animation.steps) + 1}">'
        f'<rect x="20" y="{banner_y}" width="{WIDTH - 40}" height="34" rx="17"/>'
        f'<text x="{WIDTH // 2}" y="{banner_y + 22}">{escape(animation.banner)}</text></g>',
        "</svg>",
        f"<figcaption>{escape(animation.purpose)}</figcaption>",
        "</figure>",
    ]
    return "\n".join(lines) + "\n"


def note(name: str, step: Note, x: int, y: int, index: int) -> str:
    width = round(len(step.label) * CHAR + 26)
    if x - width / 2 < 0 or x + width / 2 > WIDTH:
        raise ValueError(f"{name}: the note {step.label!r} is wider than the picture")
    tone = " alc-refused" if step.refused else ""
    return (
        f'<g class="alc-step alc-note alc-s{index}{tone}">'
        f'<rect x="{x - width // 2}" y="{y - 14}" width="{width}" height="26" rx="13"/>'
        f'<text class="alc-label" x="{x}" y="{y + 4}">{escape(step.label)}</text></g>'
    )


def arrow(name: str, step: Arrow, x1: int, x2: int, y: int, index: int) -> list[str]:
    if len(step.label) * CHAR > abs(x2 - x1) - 12:
        raise ValueError(f"{name}: the label {step.label!r} is too long for its arrow")
    way = 1 if x2 > x1 else -1
    start, end = x1 + 6 * way, x2 - 6 * way
    head = f"{end},{y} {end - 10 * way},{y - 5} {end - 10 * way},{y + 5}"
    return [
        f'<g class="alc-step alc-{step.kind} alc-s{index}">'
        f'<line x1="{start}" y1="{y}" x2="{end - 8 * way}" y2="{y}"/>'
        f'<polygon points="{head}"/>'
        f'<text class="alc-label" x="{(x1 + x2) // 2}" y="{y - 8}">{escape(step.label)}</text>'
        "</g>",
        f'<circle class="alc-dot alc-{step.kind} alc-m{index}" cx="{start}" cy="{y}" r="5"'
        f' style="--dx: {end - start}px"/>',
    ]


def describe(animation: Animation) -> str:
    """The steps in words, for screen readers."""

    def sentence(text: str) -> str:
        return text if text.endswith(("…", "?", ".")) else f"{text}."

    words = []
    for step in animation.steps:
        if isinstance(step, Note):
            words.append(sentence(f"{animation.actors[step.lane]}: {step.label}"))
        else:
            source, target = animation.actors[step.source], animation.actors[step.target]
            words.append(sentence(f"{source} to {target}: {step.label}"))
    return " ".join([*words, sentence(animation.banner)])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, animation in ANIMATIONS.items():
        (OUT / f"{name}.svg").write_text(render(name, animation), encoding="utf-8")
    print(f"{len(ANIMATIONS)} animations in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
