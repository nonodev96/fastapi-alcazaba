# API keys, revoked tokens and one-time codes

An orders API for machine clients. Their owners sign in with a code sent by e-mail
([`OneTimeCodes`](../guide/auth.md#revoked-tokens-and-one-time-codes)), rate-limited per address with
a [`FastAPIRateLimiter`](../guide/limits.md#rate-limiting), and get an access token that logging out
revokes ([`TokenBlocklist`](../guide/auth.md#revoked-tokens-and-one-time-codes)). With it they create
API keys for their programs ([`FastAPIAPIKeys`](../guide/auth.md#api-keys)): shown once, stored only
as digests, with scopes and an expiry date.

No JWT library is involved: a small stand-in signs the same claims (`sub`, `jti`, `iat`, `exp`)
with HMAC. A real app decodes its JWTs with its library and passes the blocklist the same claims.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.auth.main:app
```

Set `CACHE_SIGNING_KEY`, `OTP_SECRET`, `API_KEY_PEPPER` and `TOKEN_SECRET`
(`secrets.token_urlsafe(32)` each), the same in every process. Without them each start makes up
throwaway ones, and every token, code and key stops working when the app restarts.

## Try it

Ask for a code. The answer is the same whether the address has an account or not:

```console
$ curl -i -X POST http://127.0.0.1:8000/login/code -H "Content-Type: application/json" \
    -d '{"email": "ana@example.com"}'
HTTP/1.1 202 Accepted

{"detail":"If the address has an account, a code is on its way."}
```

The code never travels in a response. The stand-in for the e-mail service prints it in the
server's terminal:

```text
E-mail to ana@example.com: Your login code is 531060. It expires in 5 minutes.
```

Trade it for an access token. It works once:

```console
$ curl -X POST http://127.0.0.1:8000/login -H "Content-Type: application/json" \
    -d '{"email": "ana@example.com", "code": "531060"}'
{"access_token":"eyJzdWIiOiJhbmFAZXhhbXBsZS5jb20i...","expires_in":900}

$ curl -X POST http://127.0.0.1:8000/login -H "Content-Type: application/json" \
    -d '{"email": "ana@example.com", "code": "531060"}'
{"detail":"Invalid or expired code"}
```

Three codes per address every 15 minutes, wherever the requests come from. The fourth:

```console
$ curl -i -X POST http://127.0.0.1:8000/login/code -H "Content-Type: application/json" \
    -d '{"email": "ben@example.com"}'
HTTP/1.1 429 Too Many Requests
retry-after: 558

{"detail":"Too Many Requests"}
```

With the token, create two API keys, one that can only read. Each key is shown this once:

```console
$ TOKEN=eyJzdWIiOiJhbmFAZXhhbXBsZS5jb20i...
$ curl -X POST http://127.0.0.1:8000/api-keys -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d '{"scopes": ["orders:read"]}'
{"id":"j2aelpaafvjfr6ry","key":"acme_j2aelpaafvjfr6ry_gqdbzjcgz6dwuwtbmltalhn7cern6vjogztxgthxqwofrt6z2aaa","scopes":["orders:read"],"expires_at":"2027-01-24T10:55:42.303928Z"}

$ curl -X POST http://127.0.0.1:8000/api-keys -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d '{"scopes": ["orders:read", "orders:write"]}'
{"id":"mxkvocavyp6ek2dc","key":"acme_mxkvocavyp6ek2dc_hnkk5z2qld3qoc7wv5sq2aq4qrvblr7mc4bucwfvbzj5uzea6d5a","scopes":["orders:read","orders:write"],"expires_at":"2027-01-24T10:55:42.306965Z"}

$ curl -X POST http://127.0.0.1:8000/api-keys -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" -d '{"scopes": ["admin"]}'
{"detail":[{"type":"enum","loc":["body","scopes",0],"msg":"Input should be 'orders:read' or 'orders:write'",...}]}
```

Each endpoint asks for the scope it needs:

```console
$ curl -X POST http://127.0.0.1:8000/orders -H "Content-Type: application/json" \
    -H "X-API-Key: acme_mxkvocavyp6ek2dc_hnkk5z2qld3qoc7wv5sq2aq4qrvblr7mc4bucwfvbzj5uzea6d5a" \
    -d '{"item": "Olive oil", "quantity": 2}'
{"item":"Olive oil","quantity":2}

$ curl -i -X POST http://127.0.0.1:8000/orders -H "Content-Type: application/json" \
    -H "X-API-Key: acme_j2aelpaafvjfr6ry_gqdbzjcgz6dwuwtbmltalhn7cern6vjogztxgthxqwofrt6z2aaa" \
    -d '{"item": "Olive oil", "quantity": 2}'
HTTP/1.1 403 Forbidden

{"detail":"Insufficient scope"}

$ curl http://127.0.0.1:8000/orders \
    -H "X-API-Key: acme_j2aelpaafvjfr6ry_gqdbzjcgz6dwuwtbmltalhn7cern6vjogztxgthxqwofrt6z2aaa"
[{"item":"Olive oil","quantity":2}]
```

A missing, malformed, unknown, wrong or expired key gets the same 401:

```console
$ curl -i http://127.0.0.1:8000/orders -H "X-API-Key: acme_nottherightkey"
HTTP/1.1 401 Unauthorized
www-authenticate: APIKey

{"detail":"Invalid API key"}
```

Revoking a key takes effect at once in the process that handles it:

```console
$ curl -i -X DELETE http://127.0.0.1:8000/api-keys/j2aelpaafvjfr6ry -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content

$ curl -i http://127.0.0.1:8000/orders \
    -H "X-API-Key: acme_j2aelpaafvjfr6ry_gqdbzjcgz6dwuwtbmltalhn7cern6vjogztxgthxqwofrt6z2aaa"
HTTP/1.1 401 Unauthorized
www-authenticate: APIKey

{"detail":"Invalid API key"}
```

Logging out revokes the access token, which would otherwise work for 15 minutes more:

```console
$ curl -i -X POST http://127.0.0.1:8000/logout -H "Authorization: Bearer $TOKEN"
HTTP/1.1 204 No Content

$ curl -i -X POST http://127.0.0.1:8000/logout -H "Authorization: Bearer $TOKEN"
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Invalid or expired token"}
```

`/logout/everywhere` revokes every token of the user issued before the current second, and gives
the session that asked a new one. With two tokens of Ana's, `$LAPTOP` and `$PHONE`, issued a
second or more before:

```console
$ curl -X POST http://127.0.0.1:8000/logout/everywhere -H "Authorization: Bearer $PHONE"
{"access_token":"eyJzdWIiOiJhbmFAZXhhbXBsZS5jb20i...","expires_in":900}

$ curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api-keys \
    -H "Authorization: Bearer $LAPTOP" -H "Content-Type: application/json" \
    -d '{"scopes": ["orders:read"]}'
401
```

`$PHONE` gets a 401 as well, and the new token a 201.

## What to look at

- **Nothing that opens a door is stored in clear:** the `api_keys` table holds an HMAC of each
  key with the pepper, which lives outside the database, so a copy of the database can't even
  test a guess. The cache's backend holds HMACs of the codes and digests of the revoked token ids.
- **The code only goes by e-mail,** and `/login/code` answers the same for any address. The
  e-mail is sent after the response (`BackgroundTasks`), so the time it takes doesn't tell either.
- **The rate limit on codes is per address,** wherever the requests come from (`identifier`
  returns the same for everybody; the address is the `key` of `hit`). Each new code replaces the
  previous one and its five guesses, so this limit is what bounds the guesses at an account:
  3 codes × 5 guesses every 15 minutes.
- **The token check is one dependency,** `token_claims`: it decodes the token and asks the
  blocklist with its `jti`, and with its `sub` and `iat` for `/logout/everywhere`. With a JWT
  library you only change the decoding; set the blocklist's `leeway` to the one your library
  tolerates after `exp`.
- **Revoking a key** deletes its row and calls `keys.forget()`, because `cache_ttl=30` keeps
  looked-up records in each process: the other processes stop accepting it within 30 seconds.
- **Scopes are an enum:** users can only grant their keys scopes the API knows.

## The code

<!-- fmt: off -->
```python title="examples/auth/main.py"
--8<-- "examples/auth/main.py"
```
<!-- fmt: on -->
