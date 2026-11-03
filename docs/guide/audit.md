# Audit trail

In an app: [the example](../examples/audit.md). Every class and function: [the API reference](../reference/audit.md).

`FastAPIAudit` records the sensitive actions: who did what to which resource, whether it worked,
and the request it came from. As a dependency, it writes the event when the endpoint ends:

```python
from typing import Annotated

from fastapi import Depends

from fastapi_alcazaba import FastAPIAudit
from fastapi_alcazaba.audit import AuditEntry

audit = FastAPIAudit(signing_key=settings.audit_signing_key)
deleting = audit.action("invoice.delete", resource="invoice:{invoice_id}")


@app.delete("/invoices/{invoice_id}", status_code=204)
async def delete_invoice(
    invoice_id: int,
    entry: Annotated[AuditEntry, Depends(deleting)],  # first: it records refusals too
    user: Annotated[User, Depends(current_user)],
) -> None:
    entry.actor = user.id
    entry.details["amount"] = await invoices.delete(invoice_id, owner=user.id)
```

- **How it ended:** `success` when the endpoint returns, `denied` when it raises a 401 or a 403
  (1008 for WebSockets), `failure` otherwise, with the exception's type and status, never its
  message: messages carry data. Declared before the dependency that authenticates, the audit
  dependency also records the requests that one refuses, with the resource from the path. An
  endpoint that answers an error without raising sets `entry.outcome`; `entry.reason` says why.
- **Outside endpoints:** `await audit.log("user.login", outcome="failure", reason="wrong
  password", request=request)` writes one event, and `async with audit.record("backup.restore",
  actor="cron")` one for a block of code (a job, a task), which fails when it raises or is
  cancelled.
- **Correlation:** each event has the request ID (with `RequestIDMiddleware`), the OpenTelemetry
  trace, the method, the route template (`/invoices/{invoice_id}`, never the path, which may
  carry tokens) and the client's address as the server reports it (see `client_ip` behind a
  proxy).
- **No secrets:** in `details`, the values under keys that name one (`password`, `newPassword`,
  `X-Api-Key`, `refresh_token`, `session_id`, `cookie`, `card_number`…: `SENSITIVE_WORDS`, plus
  yours with `redact=["iban"]`) and the strings that look like credentials (`Bearer …`, JWTs,
  private keys) are recorded as `[redacted]`. Objects and bytes are recorded by their type, not
  their content; strings are cut at 500 characters, lists and dicts at 50 items and 4 levels.
  Give `actor` and `resource` IDs, not e-mail addresses.
- **Where they go:** to the logger `fastapi_alcazaba.audit.events`, at INFO, one line of JSON
  per event, ASCII only so that nothing in it splits a line: give it a handler of its own and let
  the log pipeline ship it to storage that can't be rewritten. If that logger outputs nothing (no
  handler, the root logger at WARNING), it says so once. `sinks=` takes others: any object with
  `async def write(event)`, such as one inserting into a table, and `MemoryAuditSink` for tests.
- **Signed and chained:** with a `signing_key` (the same in every process), each event is signed
  and carries the signature of the previous one of its stream (one per process), numbered.
  `verify_audit_trail(events, keys)` lists the events altered, forged, missing or out of their
  chain, from `AuditEvent`s or the JSON lines read back. The signature is
  `<algorithm>:<kid>:<value>` of `fastapi_alcazaba/audit3`, a NUL byte and the event's
  [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) canonical JSON without its `signature`
  (`fastapi_alcazaba.core.canonical_json`): any language with a JCS library checks it the
  same way, a SIEM or an auditor's script included. For that, details keep to I-JSON: integers
  beyond 2⁵³ are recorded as text, lone surrogates as U+FFFD. See "Verifying it" below for the
  keys and the newest events.
- **Authentication and authorization:** `FastAPIAuth(audit=audit)` records failed
  authentications, OAuth2, OpenID Connect and SAML logins, and missing scopes
  ([In the audit trail](authentication/index.md#in-the-audit-trail)); `FastAPIAuthz(audit=audit)`
  and `AuthzMiddleware(audit=audit)` the requests their policy refuses
  ([Authorization](authorization/index.md)).
- **Delivery:** a sink that fails or takes longer than `timeout` (5 s) is logged at ERROR with the
  event, so that it isn't lost, and counted; the other sinks still get it, and an event is written
  even when its request is cancelled meanwhile. The action isn't undone or refused for it: when an
  action mustn't happen unrecorded, write the record in the action's own database transaction.

## Verifying it

An HMAC secret signs and verifies alike: whoever checks the trail could rewrite it. An Ed25519
key pair separates both: the app signs with the private key, the auditor verifies with the
public one, which can't sign.

```bash
fastapi-alcazaba audit keygen audit-private.pem audit-public.pem  # the private one: 0600
```

```python
audit = FastAPIAudit(
    signing_key=settings.audit_private_key,  # PEM; or [new, old] while it's rotated
    checkpoint_interval=300,  # a checkpoint every five minutes, through the cache's backend
)
```

```bash
fastapi-alcazaba audit verify audit-2026-11-*.log --key audit-public.pem --key audit-public-2025.pem
# events: 18342 in 6 streams
# last checkpoint: 2026-11-03T17:55:02+00:00
# ok
```

- **Keys:** `signing_key` is an Ed25519 private key (PEM, or cryptography's object;
  `generate_audit_key_pair()`) or an HMAC secret of 32 bytes or more. Each signature names its
  algorithm and key (`ed25519:<kid>:…`, `hs256:<kid>:…`), so `verify_audit_trail(events, keys)`
  takes every key the events span (public keys, or the secrets) and checks each event with its
  own. **Rotating:** sign with the new key (`[new, old]`), keep giving the verifier the old public
  key for the events it signed; HMAC to Ed25519 the same way.
- **Checkpoints:** every `checkpoint_interval` seconds (with the next event), a process writes an
  `audit.checkpoint` event listing the newest event (its number and signature) of every stream
  of the trail, as each process last published it in the cache's backend (under its lock;
  `cache=`, FastAPICache's by default; without one, its own stream only). A stream whose newest
  events were removed then ends before what a later checkpoint saw, and the verifier says so; so
  it does when a whole stream is missing, or an event was replaced by one signed again. What came
  after the last checkpoint can still be removed unnoticed: `last_checkpoint(events)` (and the
  command) says when that was. Send the checkpoints also somewhere the app can't rewrite (a
  second sink), and the window shrinks to `checkpoint_interval`.
- **The command:** `fastapi-alcazaba audit verify FILE…` reads the events from JSON lines (after
  a log line's prefix too; other lines are skipped), `-` for stdin; `--key PEM` for public keys,
  `--hmac-env NAME` for a secret in an environment variable (never on the command line);
  `--json` for scripts. It exits with 1 when something's wrong.
- **From Python:**

```python
import json

from fastapi_alcazaba.audit import last_checkpoint, verify_audit_trail

with open("audit.log") as lines:
    events = [json.loads(line) for line in lines]
problems = verify_audit_trail(events, [public_key_pem, old_public_key_pem])
```
