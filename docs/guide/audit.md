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
- **Signed and chained:** with a `signing_key` (32 bytes or more, the same in every process; a
  list to rotate it, the first signs), each event is signed with HMAC-SHA256 and carries the
  signature of the previous one of its stream (one per process), numbered.
  `verify_audit_trail(events, key)` lists the events altered, forged, missing or out of their
  chain, from `AuditEvent`s or the JSON lines read back. The signature covers
  `fastapi_alcazaba/audit2`, a NUL byte and the event's
  [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) canonical JSON without its `signature`
  (`fastapi_alcazaba.core.canonical_json`): any language with a JCS library checks it the
  same way, a SIEM or an auditor's script included. For that, details keep to I-JSON: integers
  beyond 2⁵³ are recorded as text, lone surrogates as U+FFFD. It can't tell that the newest events were
  removed, and whoever has the key can rewrite the trail: keep it from the people whose actions
  are recorded.
- **Delivery:** a sink that fails or takes longer than `timeout` (5 s) is logged at ERROR with the
  event, so that it isn't lost, and counted; the other sinks still get it, and an event is written
  even when its request is cancelled meanwhile. The action isn't undone or refused for it: when an
  action mustn't happen unrecorded, write the record in the action's own database transaction.

```python
import json

from fastapi_alcazaba.audit import verify_audit_trail

with open("audit.log") as lines:
    events = [json.loads(line) for line in lines]
problems = verify_audit_trail(events, settings.audit_signing_key)
```
