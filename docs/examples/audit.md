# Audit trail

An admin API whose sensitive actions leave an audit trail: who changed which account's role or
deleted it, whether they were allowed to, and the request it came from.
[`FastAPIAudit`](../index.md#audit-trail) writes each event when the endpoint ends, as a line of
JSON on standard output, signed and chained to the previous one, and `verify_audit_trail` checks
that none was altered or removed.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.audit.main:app
```

The events are signed: set `AUDIT_SIGNING_KEY` (`secrets.token_urlsafe(32)`), the same in every
process, and keep it from the people whose actions are recorded. The example has three accounts:
`u1` (Ada, an admin, token `ada-token`), `u2` (Sam, support, token `sam-token`) and `u3` (Vic, a
viewer).

## Try it

Ada makes Vic support:

```console
$ curl -i -X PUT http://127.0.0.1:8000/accounts/u3/role -H "Authorization: Bearer ada-token" \
    -H "Content-Type: application/json" -d '{"role": "support"}'
HTTP/1.1 200 OK
x-request-id: 2b78a9c373054722779a26e2904a6b0a

{"id":"u3","email":"vic@example.com","role":"support"}
```

and the server's standard output gets the event, the request's ID included:

```json
{"id":"eec01d5969ce42deb8787aae473acfa1","time":"2026-10-26T15:44:27.175075+00:00","action":"account.role.change","outcome":"success","actor":"u1","resource":"account:u3","reason":null,"status":null,"request_id":"2b78a9c373054722779a26e2904a6b0a","trace_id":null,"method":"PUT","route":"/accounts/{account_id}/role","client":"127.0.0.1","details":{"from":"viewer","to":"support"},"stream":"1c33125463a71ecb","sequence":1,"previous":null,"signature":"13c0da20bf320784d6a1a4698cd91574b2b97b8c8e818668ef104aa9cc65fb34"}
```

Sam, who isn't an admin, tries to delete Vic's account; then someone without a token does:

```console
$ curl -i -X DELETE http://127.0.0.1:8000/accounts/u3 -H "Authorization: Bearer sam-token"
HTTP/1.1 403 Forbidden

{"detail":"Only admins can do this"}

$ curl -i -X DELETE http://127.0.0.1:8000/accounts/u3
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Not authenticated"}
```

Both are recorded as `denied`: Sam's with who he is and why, the anonymous one with the account
it was after. Each event carries the signature of the one before:

```json
{"id":"2221174595b04a82b269e441227a0933","time":"2026-10-26T15:44:27.178638+00:00","action":"account.delete","outcome":"denied","actor":"u2","resource":"account:u3","reason":"support, not admin","status":403,"request_id":"14500c6bd67ff72fd772d584cdb9fee5","trace_id":null,"method":"DELETE","route":"/accounts/{account_id}","client":"127.0.0.1","details":{},"stream":"1c33125463a71ecb","sequence":2,"previous":"13c0da20bf320784d6a1a4698cd91574b2b97b8c8e818668ef104aa9cc65fb34","signature":"41ea75d99c58cd8618d1f3e5bad140e52f270929beb26cc811a53c9bee452aa6"}
{"id":"41a26fffb4bd41df9d8c370da2d9cc7d","time":"2026-10-26T15:44:27.182414+00:00","action":"account.delete","outcome":"denied","actor":null,"resource":"account:u3","reason":"fastapi.exceptions.HTTPException","status":401,"request_id":"028f8c3e03768e47f6f85152ba18cf48","trace_id":null,"method":"DELETE","route":"/accounts/{account_id}","client":"127.0.0.1","details":{},"stream":"1c33125463a71ecb","sequence":3,"previous":"41ea75d99c58cd8618d1f3e5bad140e52f270929beb26cc811a53c9bee452aa6","signature":"be3d2ff3bb03a6642e81027d5ac0edc75356ab880f9434b2ba88a089ff91eda5"}
```

A sign-in with a wrong password records which account was tried, never the password:

```console
$ curl -i -X POST http://127.0.0.1:8000/sessions -H "Content-Type: application/json" \
    -d '{"email": "ada@example.com", "password": "hunter2"}'
HTTP/1.1 401 Unauthorized
x-request-id: b544c2688f7bd5b8cc1c58784a44ca68

{"detail":"Wrong e-mail or password"}
```

```json
{"id":"6c10e3d17e1d4e3d9442c181ebc6e525","time":"2026-10-26T15:44:27.185776+00:00","action":"session.create","outcome":"denied","actor":"u1","resource":null,"reason":"wrong password","status":null,"request_id":"b544c2688f7bd5b8cc1c58784a44ca68","trace_id":null,"method":"POST","route":"/sessions","client":"127.0.0.1","details":{},"stream":"1c33125463a71ecb","sequence":4,"previous":"be3d2ff3bb03a6642e81027d5ac0edc75356ab880f9434b2ba88a089ff91eda5","signature":"9124fbfed7d210bde4fe27842947404b9c711d86acd3b6e1469e0af09d97c7d0"}
```

Ada checks the trail (and that check is recorded too, as `audit.read`). An event altered,
forged or removed would be listed in `problems`:

```console
$ curl http://127.0.0.1:8000/audit/verify -H "Authorization: Bearer ada-token"
{"events":4,"problems":[]}
```

`GET /audit/events` lists the last events, for admins only.

## What to look at

- **The audit dependency comes first:** declared before `current_account`, it's entered before
  the authentication runs, so the 401s that one answers are recorded too, with the account from
  the path (`resource="account:{account_id}"`).
- **Who acts goes in before the check:** `require_admin` sets `entry.actor` and then refuses, so
  Sam's 403 is recorded with Sam, and `entry.reason` says why.
- **How it ended:** a 401 or a 403 is `denied`, any other exception a `failure` (a 404 for an
  account that doesn't exist), with its type and status but never its message.
- **No secrets:** the sign-in records the account and the reason, not the e-mail address typed
  (it may be a password typed in the wrong field) nor the password. Had they gone in `details`,
  the password would have been `[redacted]`, by its key.
- **A stream of its own:** the events go to the logger `fastapi_alcazaba.audit.events`, which
  writes them to standard output without the app's log format, one line each, for the log
  pipeline to ship to storage that can't be rewritten (an S3 bucket with Object Lock, a SIEM).
- **Signed and chained:** each event carries an HMAC of its content and the signature of the
  previous one of its process. `verify_audit_trail` finds an event altered, forged or missing in
  the middle; it can't tell that the newest were removed, nor stop whoever has the key.
- **Correlation:** the `x-request-id` a user quotes leads to the event (`request_id`), and with
  OpenTelemetry the event's `trace_id` leads to the request's trace.

## The code

<!-- fmt: off -->
```python title="examples/audit/main.py"
--8<-- "examples/audit/main.py"
```
<!-- fmt: on -->
