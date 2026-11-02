# Research: Every backend can do what the tools need

Findings from reading `fastapi_alcazaba/backends/` and the tools that check `supports_*`
(`response_cache/caches.py`, `background/locks.py`, `rate_limit/concurrency.py`, `auth/totp.py`,
`security/sessions.py`, `idempotency/middleware.py`), aiomcache 0.8.2 (`aiomcache/client.py`) and
memcached's protocol documentation (`protocol.txt`), on 2026-11-02.

## R1. Locks on memcached: `add`, then `gets` + `cas`

- **Decision**: take with `add` (stores only if the key is absent, atomically); release and extend
  with `gets` (value and CAS token), a comparison with the holder's token, and `cas` (stores only if
  nobody wrote since the `gets`). A release writes an empty value with an expiry in the past, so the
  key is gone at once; an extension writes the token back with the new expiry.
- **Expiry**: memcached counts whole seconds on a clock that ticks once a second, so an item set
  for N seconds lasts between N − 1 and N. A lock for `ttl` seconds is stored for
  `ceil(ttl) + 1`: never shorter than asked, at most two seconds longer.
- **The past**: aiomcache refuses a negative `exptime`; memcached reads one above 30 days as a Unix
  time, and one before the server started as "already expired". `30 days + 1` is such a time.
- **Rationale**: these are the operations memcached documents as atomic; no meta protocol (aiomcache
  doesn't speak it).
- **Alternatives considered**: `delete` to release (not conditional: could free someone else's
  lock); `touch` to extend (not conditional either).

## R2. Namespaces without listing keys: generations

- **Decision**: each namespace that was ever cleared has a generation, a random token under a key
  of its own. An entry is stored with the generations of the namespaces it belongs to; a read
  fetches the entry and those generations together and treats the entry as missing unless they all
  still match. `clear(namespace)` writes a new token.
- **The namespaces of a key**: everything before each `:` (`app:ns:abc` belongs to `app` and
  `app:ns`), so `clear()` and `clear(namespace=...)` both work, whatever the namespace. At most the
  first 8; a clear of a deeper namespace is refused with a `ValueError` (the default keys have two).
  State keys (`Cache.state_key`, `prefix!kind:...`) belong to none, as `clear` never touches them.
- **Races**: the generations are read before the entry is written; a clear in between leaves the
  entry with the old token, so it's never served (FR-005). Redis has the same window and loses it:
  `SCAN` may pass the key before it's written.
- **Rationale**: no listing, no scan; one round trip on hits (memcached's multi-get, DynamoDB's
  `BatchGetItem`); a clear costs one write however many entries it hides; nothing grows with the
  number of clears.
- **Alternatives considered**: folding the generation into the key (needs the generations before
  every read: two round trips); a list of keys per namespace (memcached caps a value at 1 MB, and an
  evicted list forgets entries, which would then be served after a clear).

## R3. What a clear returns

- **Decision**: 0 on memcached and DynamoDB, documented. `invalidate_tags_with_keys` returns no keys
  (None), which `TieredBackend` already reads as "drop every local copy".
- **Rationale**: the count needs the listing the design avoids.

## R4. Tags with generations

- **Decision**: same mechanism. `add_tags` adds each tag's generation to the entry's stamp; a read
  of a tagged entry fetches those too (a second round trip, only for tagged entries).
  `invalidate_tags` writes new tokens.
  - memcached: the stamp is rewritten with `gets` + `cas`, so a newer value written meanwhile is
    never overwritten (its own writer tags it).
  - DynamoDB: an `UpdateItem` sets the entry's `tag_generations`, only if the entry exists.
- **Eviction (memcached)**: a generation that's gone would let an entry stored before the clear
  match again ("none" then, "none" now). So on memcached every entry is stored with a real token
  for each namespace and tag: a missing one is created (`add`) first. An evicted generation then
  makes its entries miss, never come back. Entries without a stamp (0.7) are misses for the same
  reason (R5).
- **DynamoDB** never evicts, so a namespace never cleared has no generation item and costs nothing
  to write; its reads are eventually consistent, so a clear may take a moment to be seen, as any
  write to DynamoDB.

## R5. The stamp, and values written by other versions

- **memcached**: the value is the stamp, then the bytes: `\x00alcazaba-stamp:1\n`, one line of JSON
  (`{"e": expiry in Unix seconds or 0, "g": {generation key: token}}`), `\n`, the value. A value
  without the magic is from 0.7 (or another app): a miss for cached responses (R4); state keys are
  stored and read as before, so locks, counters and sessions written by 0.7 keep working, both
  ways. A malformed stamp: logged, a miss.
- **DynamoDB**: attributes beside the value: `generations` and `tag_generations` (maps). Items
  without them (0.7) are served while none of their namespaces has been cleared. A 0.7 process
  ignores them: during a rolling upgrade it may serve an entry cleared by the new version until the
  deploy ends.
- **0.7 reading a memcached value of this version**: a decode error, so a recompute; state is
  unaffected.
- **Fuzz target**: the stamp parser reads backend bytes (constitution I): `fuzz/stamps_fuzzer.py`.

## R6. Redis, Valkey, GLIDE, InMemory, Tiered

- **Decision**: unchanged. TieredBackend already forwards locks, clears and tags to its shared
  backend and copies its `supports_*`.

## R7. Refusing at setup

- **Decision**: after this change every backend shipped with the package supports locks, tags and
  counters. The tools that already refuse a backend without locks (`Lock`, the concurrency limiter,
  `FastAPITOTP`) keep doing so at setup, and their messages say that every shipped backend can.
  `distributed_lock` keeps its warning (it's a performance option, not a guarantee).
- **Left as it is**: `@cache(tags=...)` on a backend written by the app without tags logs a warning
  when storing. The decorator is applied before the backend is configured (`FastAPICache.init` runs
  at startup), so it can't refuse earlier, and refusing on a request is what FR-008 forbids.

## R8. Performance

- memcached hit: one multi-get (entry + up to 2 generations for the default keys), a 20-byte magic
  check and a short JSON parse. Miss: one more multi-get for the generations, and an `add` per
  generation the first time.
- DynamoDB hit: `BatchGetItem` instead of `GetItem` for entries in a namespace; each generation
  read costs a read unit.
