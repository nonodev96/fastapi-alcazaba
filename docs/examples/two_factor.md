# Second factor

--8<-- "docs/examples/animations/two_factor.svg"

An authenticator app's codes after the password, recovery codes for a lost phone, and a recent
code to delete the account. [`FastAPITOTP`](../guide/two-factor.md) checks the codes, each once,
and keeps no secret in clear.

Ana logs in with her password, `correct horse battery staple`, adds an authenticator app, and
from then on logs in with her password and a code of it.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.two_factor.main:app
```

Set `TOTP_SECRET` and `CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)` each), the same in every
process. curl keeps `Secure` cookies from `localhost` too, so a cookie jar plays the browser; the
`Sec-Fetch-Site` header is the one browsers send with the app's own requests, which
`CSRFMiddleware` checks.

## Try it

Ana logs in with her password; she has no second factor yet, and deleting her account needs a
recent one:

```console
$ curl -c jar -b jar -X POST http://localhost:8000/login -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" -d '{"username": "ana", "password": "correct horse battery staple"}'
{"logged_in":"ana"}

$ curl -i -c jar -b jar -X DELETE http://localhost:8000/account -H "Sec-Fetch-Site: same-origin"
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"Second factor required"}
```

She adds her app: the URI is the QR code to scan, the secret the same for typing it.

```console
$ curl -c jar -b jar -X POST http://localhost:8000/totp/enroll -H "Sec-Fetch-Site: same-origin"
{"uri":"otpauth://totp/Example:ana?secret=6Q3EIKOERS55HPGZCFGY6JPIBDYPHA7T&issuer=Example&algorithm=SHA1&digits=6&period=30","secret":"6Q3EIKOERS55HPGZCFGY6JPIBDYPHA7T"}
```

The first code the app shows turns it on, and gives her recovery codes, once:

```console
$ curl -c jar -b jar -X POST http://localhost:8000/totp/confirm -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" -d '{"code": "013052"}'
{"recovery_codes":["F5ZTX-ZHQWF","UPK44-FTYCS","A43YR-J74Z7","TPQFB-GGZMD","FVME9-D7SJF","AQNE4-7BV6E","PX6MR-VW6EF","3E6CM-692RA","3YMDQ-3TU9Y","4JZGZ-44RC9"]}
```

In another browser (another cookie jar), her password only gets her halfway in:

```console
$ curl -c jar2 -b jar2 -X POST http://localhost:8000/login -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" -d '{"username": "ana", "password": "correct horse battery staple"}'
{"next":"/login/totp"}

$ curl -i -c jar2 -b jar2 http://localhost:8000/me
HTTP/1.1 401 Unauthorized
content-type: application/json

{"detail":"Not logged in"}

$ curl -c jar2 -b jar2 -X POST http://localhost:8000/login/totp -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" -d '{"code": "013263"}'
{"logged_in":"ana"}

$ curl -c jar2 -b jar2 http://localhost:8000/me
{"username":"ana","second_factor":true}
```

The same code, from someone who saw it, is refused:

```console
$ curl -c jar3 -b jar3 -X POST http://localhost:8000/login -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" -d '{"username": "ana", "password": "correct horse battery staple"}'
{"next":"/login/totp"}

$ curl -i -c jar3 -b jar3 -X POST http://localhost:8000/login/totp -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" -d '{"code": "013263"}'
HTTP/1.1 401 Unauthorized
content-type: application/json

{"detail":"Wrong code"}
```

Without her phone, a recovery code logs her in, once; it's a second factor too, recent enough
to delete the account:

```console
$ curl -c jar3 -b jar3 -X POST http://localhost:8000/login/recovery -H "Sec-Fetch-Site: same-origin" -H "Content-Type: application/json" -d '{"code": "F5ZTX-ZHQWF"}'
{"logged_in":"ana","recovery_codes_left":9}

$ curl -i -c jar3 -b jar3 -X DELETE http://localhost:8000/account -H "Sec-Fetch-Site: same-origin"
HTTP/1.1 204 No Content
```

## What to look at

- **Nothing in clear:** the database keeps the secret sealed for Ana (`user.totp`) and the
  recovery codes' digests; the enrollment waits sealed in the session until a code of it is
  confirmed.
- **Halfway in:** after the password, the session holds `pending`, not `user_id`: `/me` and the
  other routes for logged-in users don't take it.
- **Each code once:** across browsers and processes (in the cache's backend), and wrong ones
  are counted: five, and Ana's codes are refused for 15 minutes.
- **Step-up:** `DELETE /account` needs a code of the last five minutes in this session, Ana's:
  a session left open isn't enough.
- **The audit trail:** each code checked is an event (`auth.mfa`, `auth.recovery_code`), with
  the outcome and the request.

## The code

<!-- fmt: off -->
```python title="examples/two_factor/main.py"
--8<-- "examples/two_factor/main.py"
```
<!-- fmt: on -->
