# UnkeyStrategy

!!! warning "Experimental"
    New in 0.6: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../../features/scope.md) lists the tools that are settled.

API keys that [Unkey](https://www.unkey.com) keeps and verifies, with their permissions, credits
and rate limits: `pip install "fastapi-alcazaba[auth-strategy-unkey]"`, which installs httpx2.

In an app: [the example](../../examples/auth_strategies/unkey.md). How strategies are tried: [Strategies](index.md#strategies). Every argument: [the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.strategies.unkey.UnkeyStrategy).

`UnkeyStrategy` reads the key of `Authorization: Bearer` (or of a header, `header="X-API-Key"`)
and asks Unkey's API to verify it (`keys.verifyKey`, v2) on every request. `UnkeyKeys` issues and
deletes them, for the page where your users make theirs.

```python
from fastapi_alcazaba.auth import FastAPIAuth
from fastapi_alcazaba.auth.strategies import UnkeyStrategy
from fastapi_alcazaba.auth.strategies.unkey import UnkeyClient, UnkeyKey, UnkeyKeys

# A root key for each use, with only the permissions it needs.
verifier = UnkeyClient(settings.unkey_verify_key)  # may only verify keys
keys = UnkeyKeys(UnkeyClient(settings.unkey_admin_key), api_id=settings.unkey_api_id)


async def load_user(key: UnkeyKey) -> User | None:
    return await db.users.get(key.external_id)  # the ID given when the key was made


auth = FastAPIAuth(
    UnkeyStrategy(verifier, load_user, permissions="documents.read"),
    UnkeyStrategy(verifier, load_user, name="unkey-write", permissions="documents.write", cost=5),
)


@app.post("/documents", dependencies=[Depends(auth.authenticate("unkey-write"))])
async def write_document(document: Document) -> Document: ...
```

- **What Unkey answers:** `VALID` gives the user, `verify(key)` with an `UnkeyKey` (its id,
  `external_id`, `meta`, credits left, permissions, roles, rate limits), or the `UnkeyKey` itself
  without `verify`. A key that doesn't exist, is disabled or expired fails, a 401, or the next
  strategy is tried. One without the permissions (`permissions=`, Unkey's query syntax: `"a AND
  (b OR c)"`) gets a 403; one over its rate limit, a 429 with `Retry-After`; one out of credits,
  a 429, or a 402 with `credits_status=402`. A code this version doesn't know fails, with a
  warning: nothing but `VALID` lets a request through.
- **Each route its permissions:** a strategy has one query and one `cost`; a route that needs
  others has a strategy of its own, with another `name` and the same client.
- **Credits and rate limits:** each verification spends `cost` credits (1 by default, 0 for
  none) of keys that have a limit, and counts in the key's rate limits; `ratelimits=["tokens"]`
  applies others by name. Nothing is cached: a key deleted or disabled is refused at once (Unkey
  takes up to 30 seconds to spread a deletion), and the credits are right.
- **Fails closed:** each call has a deadline (`timeout`, 2 seconds) and Unkey a circuit breaker
  (`ResilientTransport`). Unkey down, slow or limiting the workspace (its 429) is a 503 for the
  request (1013 for a WebSocket), never let through. A root key that may not verify raises
  `UnkeyError`: a 500, a mistake to fix.
- **Not sent:** a key that isn't a token (spaces, non-ASCII, longer than 512 characters) fails
  without reaching Unkey, and no key is ever logged.
- **Counted:** each verification in `fastapi_alcazaba.auth.unkey`, by strategy and Unkey's code,
  and `unavailable` when it didn't answer.

## Issuing and deleting keys

```python
created = await keys.create(
    external_id=user.id,
    name="Ana's laptop",
    credits_left=1_000,
    refill=CreditRefill(1_000, day=1),  # back to 1,000 on the 1st of each month
    ratelimits=[KeyRatelimit("requests", 100, timedelta(minutes=1))],
    permissions=["documents.read"],
    expires=datetime(2027, 1, 1, tzinfo=UTC),
)
show_once(created.key.get_secret_value())  # Unkey keeps only its hash
await db.keys.add(user.id, created.key_id)

await keys.delete(created.key_id)  # its verifications fail within 30 seconds
```

- **Shown once:** `created.key` is a `SecretStr`, kept out of logs and reprs: show it to its owner
  and forget it. Keep `key_id`, to delete it later, and check that a key belongs to the user who
  asks to delete it.
- **Two root keys:** give `UnkeyStrategy` a root key that may only verify, and `UnkeyKeys`
  another that may create and delete: a leak of the first can't make keys.
- **Close them:** `await client.aclose()` in the lifespan, at shutdown, for the clients
  `UnkeyClient` made.
