# Accounting

--8<-- "docs/examples/animations/accounting.svg"

An API that meters what each tenant uses, holds it to its plan, and records every charge for
billing. [`FastAPIAccounting`](../guide/accounting.md) charges each search before it runs, and
the tokens of each summary when it answers; `GET /usage` tells the tenant what it has left.

Each tenant has an API key and a plan: `acme-key` (Acme, the free plan: 3 searches a day and 50
tokens a month) and `globex-key` (Globex, the pro plan: searches without a limit and 10,000
tokens a month).

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.accounting.main:app
```

## Try it

Acme searches three times today; the responses say what's left, and the fourth gets a 429 until
midnight, UTC:

```console
$ curl -i "http://127.0.0.1:8000/search?q=ore" -H "X-API-Key: acme-key"
HTTP/1.1 200 OK
content-type: application/json
ratelimit: "searches";r=0;t=43491
ratelimit-policy: "searches";q=3;w=86400

["Oregano"]

$ curl -i "http://127.0.0.1:8000/search?q=oli" -H "X-API-Key: acme-key"
HTTP/1.1 429 Too Many Requests
ratelimit: "searches";r=0;t=43491
ratelimit-policy: "searches";q=3;w=86400
retry-after: 43491
content-type: application/json

{"detail":"Quota exceeded"}
```

Globex, on the pro plan, has no limit, and no `RateLimit` headers:

```console
$ curl "http://127.0.0.1:8000/search?q=oli" -H "X-API-Key: globex-key"
["Olive oil","Olives"]
```

A summary takes the tokens it used, known only when it answers. Acme's second one starts with
18 tokens left and uses 32, which it's charged: the quota is checked when a request starts. The
next one gets a 402, a plan to upgrade:

```console
$ curl -X POST http://127.0.0.1:8000/summaries -H "X-API-Key: acme-key" -H "Content-Type: application/json" -d '{"text": "Olive oil is pressed from olives, the fruit of the olive tree, and keeps for two years in the dark."}'
{"summary":"Olive oil is pressed from olives, the fruit of the olive tree,","tokens":32}

$ curl -i -X POST http://127.0.0.1:8000/summaries -H "X-API-Key: acme-key" -H "Content-Type: application/json" -d '{"text": "Olives."}'
HTTP/1.1 402 Payment Required
ratelimit: "tokens";r=0;t=2549091
ratelimit-policy: "tokens";q=50
retry-after: 2549091
content-type: application/json

{"detail":"Quota exceeded"}
```

What Acme has used, and when each meter starts again:

```console
$ curl http://127.0.0.1:8000/usage -H "X-API-Key: acme-key"
{"searches":{"used":3,"limit":3,"remaining":0,"period":"2026-11-01","resets":"2026-11-02T00:00:00+00:00"},"tokens":{"used":64,"limit":50,"remaining":0,"period":"2026-11","resets":"2026-12-01T00:00:00+00:00"}}
```

Every charge is a line of JSON on the server's output, for billing to add up:

```json
{"id":"165fc663b5ff4376a6cb76959df5df3a","time":"2026-11-01T11:55:09.237011+00:00","subject":"acme","meter":"tokens","amount":32,"period":"2026-11","request_id":null,"trace_id":null,"method":"POST","route":"/summaries"}
```

## What to look at

- **The plan decides:** each quota's `limit` is a function of the tenant (`searches_of`,
  `tokens_of`), which reads its plan; None from it is no limit.
- **Charged before, or as it goes:** a search is charged before it runs, checked and counted at
  once, so concurrent searches never go past the quota. The tokens are added by the endpoint
  (`usage.add(...)`) and charged when it answers.
- **429 or 402:** over the searches of the day, try again tomorrow; over the tokens of the
  month, a plan to upgrade (`status=402`).
- **Counters and records:** the counters, in the backend, hold the quotas; the records, one per
  charge with an `id` to count it once, are what billing adds up, and they're written even when
  the backend is down.

## The code

<!-- fmt: off -->
```python title="examples/accounting/main.py"
--8<-- "examples/accounting/main.py"
```
<!-- fmt: on -->
