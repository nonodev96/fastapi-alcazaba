# Performance

What the hot paths cost, measured by the benchmarks in
[`tests/benchmarks`](https://github.com/nonodev96/fastapi-alcazaba/blob/main/tests/benchmarks/test_benchmarks.py),
which CI runs on every push and fails when one gets more than 50 % slower than the others.

On an AMD Ryzen 9 9950X, Python 3.13, pydantic 2.14 and the in-memory backend, the median of each
(one call; the time of the event loop's own round trip is included):

| What | Median | Per second |
| --- | ---: | ---: |
| A cache hit: 100 pydantic items, as their type | 78 µs | 12 400 |
| The same, signed (HMAC-SHA256) | 84 µs | 11 700 |
| The same, encrypted (AES-256-GCM) | 84 µs | 11 600 |
| The same, compressed with zstd | 81 µs | 12 100 |
| The same, encrypted and compressed | 88 µs | 11 200 |
| The function without the cache, returning the items it holds | 4 µs | 238 000 |
| Encoding the 100 items as JSON | 62 µs | 16 100 |
| Decoding them back as `list[Item]` | 48 µs | 20 800 |
| The default cache key of an endpoint | 1 µs | 990 000 |
| Signing and verifying the 100 items | 6 µs | 155 000 |
| Encrypting and decrypting them | 8 µs | 129 000 |
| A rate limiter's check, within the limit | 24 µs | 41 000 |

- **Most of a hit is pydantic's:** turning JSON back into 100 models takes 48 of the 78 µs.
  Signing adds 6 µs, encryption 6 more, compression 3: protecting the entries costs little next
  to decoding them.
- **When the cache pays:** a function that answers in 4 µs from memory is faster without it.
  One that queries a database (a millisecond or more) or another service is a hundred times
  slower than a hit. Cache the second kind.
- **With Redis or Valkey,** add a network round trip (a fraction of a millisecond on the same
  network) to each hit and each rate-limit check.

Run them on your machine:

```bash
make deps
.venv/bin/pytest tests/benchmarks --benchmark-only
```
