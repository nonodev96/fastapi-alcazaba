# API keys, tokens and codes

Credentials kept only as digests: API keys with scopes and expiry, revoked tokens and one-time
codes.

In an app: [the example](../examples/auth.md). Every class and function: [the API reference](../reference/auth.md).

## API keys

`FastAPIAPIKeys` authenticates machine clients by an API key. It generates the keys and checks
them; your database keeps a record of each, never the key itself:

```python
from fastapi_alcazaba.auth import FastAPIAPIKeys


async def load_key(key_id: str) -> ApiKeyRow | None:  # any object with digest, scopes, expires_at
    return await db.get(ApiKeyRow, key_id)


keys = FastAPIAPIKeys(load_key, pepper=settings.api_key_pepper, bearer=True)


@app.post("/api-keys")
async def create_key(user: Annotated[User, Depends(current_user)]) -> dict[str, str]:
    new = keys.generate(scopes=["orders:read"], expires_at=datetime.now(UTC) + timedelta(days=90))
    await db.add(ApiKeyRow(owner=user.id, **dataclasses.asdict(new.record)))
    return {"key": new.key}  # shown once: only its digest is stored


@app.get("/orders")
async def orders(key: Annotated[ApiKeyRow, Depends(keys.require("orders:read"))]) -> list[Order]:
    return await db.orders(owner=key.owner)
```

- **Key format:** `ak_<id>_<secret>` (`prefix` changes `ak`; `sk_live` works too): an 80-bit id
  that finds the record and a 256-bit secret, in lowercase base32. A distinctive prefix lets
  secret scanners (GitHub's, GitLab's) spot leaked keys.
- **Stored as a digest:** the record holds a SHA-256 digest of the secret, bound to the key's id.
  Random 256-bit secrets don't need a slow password hash. With a `pepper` (a server secret of at
  least 32 bytes, kept out of the database) the digest is an HMAC, so a copy of the database
  alone can't even test a guess. A list of peppers rotates them: the first one makes new keys,
  all of them are accepted.
- **Your database:** `lookup(key_id)` returns your row, async or not (a plain function runs in a
  thread, like FastAPI's sync dependencies), and the dependency returns that same row. Any object
  with `digest`, `scopes` (a collection, or a space-separated string) and `expires_at` works;
  `APIKeyRecord` is what `generate()` gives.
- **Where the key comes from:** the `X-API-Key` header (`header=...`) and, with `bearer=True`,
  `Authorization: Bearer <key>`. Both appear in the OpenAPI schema, for the docs' Authorize
  button.
- **One answer:** a missing, malformed, unknown, wrong or expired key gets the same 401, with
  `WWW-Authenticate`; the secret is compared in constant time, and malformed keys don't reach
  `lookup`. `keys.require("a", "b")` adds a 403 for a valid key without all those scopes (with an
  RFC 6750 `insufficient_scope` challenge in bearer mode). Keys and secrets are never logged.
- **Revocation:** delete the record, or set its `expires_at` in the past (naive datetimes are
  taken as UTC). `cache_ttl` keeps looked-up records in each process for that many seconds to
  spare the database, so a revocation then takes up to `cache_ttl` to reach every process;
  `keys.forget(key_id)` drops one from this process at once. Unknown ids are never cached.
- **Floods:** each request with a well-formed key costs a lookup: put a `FastAPIRateLimiter`
  in front.
- **Without a dependency:** `await keys.authenticate(key)` for keys that come some other way (a
  WebSocket message).

## Revoked tokens and one-time codes

A JWT can't be taken back once issued. `TokenBlocklist` keeps the revoked ones in the cache's
backend, shared by every process, until they would have expired anyway:

```python
from fastapi_alcazaba.auth import OneTimeCodes, TokenBlocklist

blocklist = TokenBlocklist()  # FastAPICache's backend; or TokenBlocklist(cache)


async def current_user(claims: Annotated[dict[str, Any], Depends(decode_jwt)]) -> str:
    sub, iat = claims["sub"], claims["iat"]
    if await blocklist.is_revoked(claims["jti"], subject=sub, issued_at=iat):
        raise HTTPException(401)
    return sub


@app.post("/logout")
async def logout(claims: Annotated[dict[str, Any], Depends(decode_jwt)]) -> None:
    await blocklist.revoke(claims["jti"], expires_at=claims["exp"])


@app.post("/password")
async def change_password(user: Annotated[str, Depends(current_user)]) -> Token:
    ...
    await blocklist.revoke_subject(user, ttl=TOKEN_LIFETIME)  # log out everywhere
    return issue_token(user)  # issued now: still valid
```

`OneTimeCodes` are the codes you send by e-mail or SMS:

```python
codes = OneTimeCodes(secret=settings.otp_secret)  # secrets.token_urlsafe(32)


@app.post("/login/code", dependencies=[Depends(FastAPIRateLimiter(times=3, minutes=15))])
async def send_code(email: str) -> None:
    await send_email(email, await codes.issue(email, purpose="login"))


@app.post("/login")
async def login(email: str, code: str) -> Token:
    if not await codes.verify(email, code, purpose="login"):
        raise HTTPException(400, "Invalid or expired code")
    ...
```

- **Nothing in clear:** token ids and subjects are stored as SHA-256 digests. Codes are stored as
  an HMAC with `secret`, bound to their subject and purpose: a plain digest of a 6-digit code is
  brute-forced in milliseconds by whoever reads the backend. A list of secrets rotates them.
- **Expiry:** each revocation lasts while the token could still be accepted (`exp`, plus
  `leeway`, 60 s by default, for what your JWT library tolerates). Codes last `ttl` seconds (300).
- **Log out everywhere:** `revoke_subject` refuses the tokens issued before the current second.
  `iat` has whole seconds, so the token you issue right after for the current session stays
  valid.
- **Guesses and single use:** every guess counts, malformed ones included, and a code is lost
  after `max_attempts` (5). Of several requests with the right code at the same time, only one
  succeeds. Issuing a code replaces the previous one and gives the attempts again: rate-limit the
  route that issues them.
- **Every process sees them at once:** they're read from the shared backend, never from
  `TieredBackend`'s copies in each process. On DynamoDB, whose reads are eventually consistent, a
  revocation or a new code can take about a second to reach every process.
- **Failures:** if the backend fails, `is_revoked` refuses tokens (`fail_open=True` accepts them),
  `verify` returns False, and `revoke`, `revoke_subject` and `issue` raise `BackendUnavailable`.
  With a `signing_key` or `encryption_key`, revocations are sealed and planted ones are ignored.
  Whoever can delete keys in the backend can still un-revoke tokens.
