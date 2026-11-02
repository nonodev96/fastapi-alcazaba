# Quickstart: checking whole cached responses

## Prerequisites

`make deps`; for the live checks, `make services-up`.

## Automated

```shell
.venv/bin/pytest tests/unit/test_cached_responses.py      # Stories 1–3 and edge cases
.venv/bin/pytest tests/unit/test_fuzz_targets.py          # the entry parser's fuzz target
make services-up && make test-integration                 # the envelope on live backends
make lint && .venv/bin/coverage run -m pytest && .venv/bin/coverage report
```

## By hand

An app with three endpoints (see [the contract](contracts/cache-decorator.md)):

- `GET /logo.png` returning `Response(png_bytes, media_type="image/png")` under `@cache(expire=60)`;
- `GET /users` returning `JSONResponse(users, headers={"X-Total-Count": "2"})`;
- `GET /me` with `response_model=Public` returning a dict with an extra `password_hash`.

Expected, requesting each twice with `curl -i`:

| Endpoint | Second request |
| --- | --- |
| `/logo.png` | `Cache-Status: ...; hit`, same bytes, `Content-Type: image/png` |
| `/users` | `hit`, `X-Total-Count: 2` |
| `/me` | `hit`; the backend's entry has no `password_hash` |

And `@cache(expire=60, cache_control="public, s-maxage=600")` sends
`public, s-maxage=600, max-age=<remaining>` on both.
