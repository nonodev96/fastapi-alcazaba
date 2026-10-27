"""An audit trail of the sensitive actions: who did what to which resource, whether it worked, and
the request it came from.

    audit = FastAPIAudit(signing_key=settings.audit_signing_key)

    @app.delete("/invoices/{invoice_id}")
    async def delete_invoice(
        invoice_id: int,
        entry: Annotated[
            AuditEntry, Depends(audit.action("invoice.delete", resource="invoice:{invoice_id}"))
        ],
        user: Annotated[User, Depends(current_user)],
    ) -> None:
        entry.actor = user.id
        ...

The event is written when the endpoint ends: `success` when it returns, `denied` when it raises a
401 or a 403, `failure` otherwise, with the exception's type and status, never its message.
Declared before the dependency that authenticates, as here, it also records the requests that one
refuses. Elsewhere (a login form, a job), `await audit.log(...)` writes one event, and `async with
audit.record(...)` one for a block of code.

Events go to the `fastapi_alcazaba.audit.events` logger by default, one JSON object per line, for
the log pipeline to keep (CloudWatch, Loki, a SIEM...); a sink of your own (`AuditSink`) can write
them elsewhere, such as a table.

What goes in, and what doesn't:

- Correlation: the request ID (`RequestIDMiddleware`), the trace (OpenTelemetry), the method, the
  route (`/invoices/{invoice_id}`; never the path, which may carry tokens) and the client's
  address as the server reports it (behind a proxy, see `client_ip`).
- No secrets: in `details`, the values under keys that name one (password, token, api_key,
  authorization, cookie, session...) and the strings that look like credentials (`Bearer ...`,
  JWTs, private keys) become "[redacted]". Values that aren't JSON (objects, bytes) are recorded
  by their type, strings are cut at 500 characters, and containers at 50 items and 4 levels.
- Integrity: with a `signing_key`, each event is signed (HMAC-SHA256 of its RFC 8785 canonical
  JSON, which any language can compute again) and chained to the previous one of its stream (one
  per process), and `verify_audit_trail()` finds the events altered, forged, removed or reordered.
  It can't tell that the newest events were removed, and whoever has the key can rewrite the
  trail: keep it from those whose actions are recorded.
- Delivery: a sink that fails, or takes longer than `timeout`, is logged with the event (so that
  it isn't lost) and counted, and the other sinks still get it. The action isn't undone or refused
  for it: when an action mustn't happen unrecorded, write the record in its database transaction.
"""

import asyncio
import dataclasses
import decimal
import enum
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import string
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any, Literal, Protocol, get_args

from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from starlette.exceptions import HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.status import WS_1008_POLICY_VIOLATION

from fastapi_alcazaba.core.canonical import MAX_SAFE_INTEGER, canonical_json
from fastapi_alcazaba.core.integrity import Secrets, secret_list
from fastapi_alcazaba.observability.request_id import request_id
from fastapi_alcazaba.telemetry.metrics import Metrics
from fastapi_alcazaba.telemetry.tracing import traceparent

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: How an action ended.
Outcome = Literal["success", "failure", "denied"]
OUTCOMES: frozenset[str] = frozenset(get_args(Outcome))

#: Longest string recorded in `details`, `reason`, `actor` and `resource`; the rest is cut.
MAX_STRING = 500
#: Most items recorded of a list or a dict in `details`.
MAX_ITEMS = 50
#: Deepest nesting recorded in `details`.
MAX_DEPTH = 4

#: Words that, in a key of `details`, mark its value as a secret: compared with the key's words
#: (`newPassword`, `X-Api-Key`, `refresh_tokens`...) and with each pair of them joined (`api` and
#: `key`).
SENSITIVE_WORDS: frozenset[str] = frozenset(
    {
        "accesskey",
        "apikey",
        "auth",
        "authorization",
        "cardnumber",
        "cookie",
        "credential",
        "cvc",
        "cvv",
        "otp",
        "passphrase",
        "passwd",
        "password",
        "pin",
        "privatekey",
        "pwd",
        "secret",
        "secretkey",
        "session",
        "signature",
        "token",
    }
)

REDACTED = "[redacted]"

_ACTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_CREDENTIAL = re.compile(
    r"^\s*(?:bearer|basic|digest)\s+[\w.~+/=-]+\s*$"  # an Authorization header's value
    r"|eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\."  # a JWT
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
# What the signatures cover starts with this, then comes the event's RFC 8785 JSON.
_CONTEXT = b"fastapi_alcazaba/audit2\0"
# Lone surrogates (text decoded with errors="surrogateescape") have no UTF-8.
_SURROGATES = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")


def _check_action(name: str) -> None:
    if not (isinstance(name, str) and _ACTION.fullmatch(name)):
        raise ValueError(
            f"invalid action {name!r}: up to 100 letters, digits, '.', '_', ':' and '-', "
            "starting with a letter or a digit (e.g. 'invoice.delete')"
        )


def _check_outcome(outcome: str | None) -> None:
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"the outcome is one of {sorted(OUTCOMES)}, not {outcome!r}")


def _cut(text: str) -> str:
    text = _SURROGATES.sub("\ufffd", text)
    if len(text) <= MAX_STRING:
        return text
    return f"{text[:MAX_STRING]}…[{len(text) - MAX_STRING} more]"


def _identifier(value: str | int | uuid.UUID | None, what: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | uuid.UUID):
        raise TypeError(f"the {what} is a string, an integer or a UUID, not {type(value).__name__}")
    return _cut(str(value))


def _words(key: str) -> set[str]:
    """The words of a key, singular, and each pair of them joined: `X-Api-Keys` gives x, api,
    key, xapi and apikey."""
    words = [
        word[:-1] if len(word) > 3 and word.endswith("s") else word
        for word in _WORD.findall(_CAMEL.sub(r"\1 \2", key).lower())
    ]
    return {*words, *(first + second for first, second in zip(words, words[1:], strict=False))}


def _type_name(value: object) -> str:
    kind = type(value)
    if kind.__module__ == "builtins":
        return kind.__qualname__
    return f"{kind.__module__}.{kind.__qualname__}"


class _Cleaner:
    """Turns `details` into what's recorded: JSON values, no secrets, bounded."""

    def __init__(self, sensitive: frozenset[str]) -> None:
        self.sensitive = sensitive

    def details(self, details: Mapping[str, Any]) -> dict[str, Any]:
        return self._mapping(details, 0)

    def value(self, value: Any, depth: int) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            number = int(value)  # an IntEnum's value
            # Beyond 2**53 JSON parsers round integers: kept exact, as text.
            return str(number) if abs(number) > MAX_SAFE_INTEGER else number
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, str):
            return REDACTED if _CREDENTIAL.search(value) else _cut(str.__str__(value))
        if isinstance(value, enum.Enum):
            return self.value(value.value, depth)
        if isinstance(value, datetime | date | time):
            return value.isoformat()
        if isinstance(value, uuid.UUID | decimal.Decimal):
            return str(value)
        if isinstance(value, bytes | bytearray | memoryview):
            return f"<{len(value)} bytes>"  # never the bytes: they may be anything
        if isinstance(value, Mapping | list | tuple | set | frozenset):
            if depth >= MAX_DEPTH:
                return "[too deep]"
            if isinstance(value, Mapping):
                return self._mapping(value, depth)
            items = [self.value(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
            if len(value) > MAX_ITEMS:
                items.append(f"[{len(value) - MAX_ITEMS} more]")
            return items
        return f"<{_type_name(value)}>"  # its repr could hold anything, a password included

    def _mapping(self, value: Mapping[Any, Any], depth: int) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index == MAX_ITEMS:
                cleaned["…"] = f"[{len(value) - MAX_ITEMS} more]"
                break
            if isinstance(key, str | int) and not isinstance(key, bool):
                name = _cut(str(key))
            else:
                name = f"<{_type_name(key)}>"
            secret = isinstance(key, str) and not self.sensitive.isdisjoint(_words(key))
            cleaned[name] = REDACTED if secret else self.value(item, depth + 1)
        return cleaned


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """An event of the trail, as the sinks get it; `to_dict()` and `to_json()` give its JSON form.

    :param id: random, unique.
    :param time: when it was written (UTC).
    :param action: what was done, e.g. "invoice.delete".
    :param outcome: success, failure or denied.
    :param actor: who did it (a user's ID), if known.
    :param resource: what it was done to, e.g. "invoice:42".
    :param reason: why it failed or was denied: the exception's type, or the reason given.
    :param status: the HTTP status of the exception that ended it.
    :param request_id: the request's ID (`RequestIDMiddleware`).
    :param trace_id: the OpenTelemetry trace it belongs to.
    :param method: the request's HTTP method.
    :param route: the template of the route that matched, e.g. "/invoices/{invoice_id}".
    :param client: the client's address, as the server reports it.
    :param details: anything else, redacted and bounded.
    :param stream: the writer's stream: one per process and FastAPIAudit.
    :param sequence: its number in the stream, from 1.
    :param previous: the signature of the stream's previous event (with a `signing_key`).
    :param signature: with a `signing_key`, HMAC-SHA256 in hex of `fastapi_alcazaba/audit2`, a
        NUL byte, and the RFC 8785 canonical JSON of `to_dict()` without `signature`: a verifier
        in any language computes the same.
    """

    id: str
    time: datetime
    action: str
    outcome: Outcome
    actor: str | None = None
    resource: str | None = None
    reason: str | None = None
    status: int | None = None
    request_id: str | None = None
    trace_id: str | None = None
    method: str | None = None
    route: str | None = None
    client: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    stream: str = ""
    sequence: int = 0
    previous: str | None = None
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {item.name: getattr(self, item.name) for item in dataclasses.fields(self)}
        data["time"] = self.time.isoformat()
        data["details"] = dict(self.details)
        return data

    def to_json(self) -> str:
        """One line of JSON, ASCII only: nothing in it can split a log line."""
        return json.dumps(self.to_dict(), separators=(",", ":"))


def _mac(key: bytes, data: Mapping[str, Any]) -> str:
    return hmac.new(key, _CONTEXT + canonical_json(data), hashlib.sha256).hexdigest()


def _signed_by(key: bytes, data: Mapping[str, Any], signature: bytes) -> bool:
    """Whether `signature` is `key`'s for `data`. As bytes: compare_digest refuses strings with
    non-ASCII characters, which a line tampered with may have."""
    try:
        expected = _mac(key, data).encode()
    except (ValueError, TypeError, RecursionError):  # nothing an event of ours holds
        return False
    return hmac.compare_digest(signature, expected)


class AuditEntry:
    """The event an action writes when it ends: the endpoint fills in what it learns.

    `outcome` is decided by how the action ends; set it ("denied", "failure") when the endpoint
    returns an error response instead of raising one. `reason` says why it failed or was denied,
    the exception's type by default.
    """

    __slots__ = ("_actor", "_outcome", "_reason", "_resource", "action", "details")

    def __init__(
        self,
        action: str,
        *,
        actor: str | int | uuid.UUID | None = None,
        resource: str | int | uuid.UUID | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        _check_action(action)
        self.action = action
        self.actor = actor
        self.resource = resource
        self.details: dict[str, Any] = dict(details or {})
        self._outcome: Outcome | None = None
        self._reason: str | None = None

    @property
    def actor(self) -> str | None:
        """Who does it, e.g. the user's ID."""
        return self._actor

    @actor.setter
    def actor(self, value: str | int | uuid.UUID | None) -> None:
        self._actor = _identifier(value, "actor")

    @property
    def resource(self) -> str | None:
        """What it's done to, e.g. "invoice:42"."""
        return self._resource

    @resource.setter
    def resource(self, value: str | int | uuid.UUID | None) -> None:
        self._resource = _identifier(value, "resource")

    @property
    def outcome(self) -> Outcome | None:
        return self._outcome

    @outcome.setter
    def outcome(self, value: Outcome | None) -> None:
        _check_outcome(value)
        self._outcome = value

    @property
    def reason(self) -> str | None:
        return self._reason

    @reason.setter
    def reason(self, value: str | None) -> None:
        if value is not None and not isinstance(value, str):
            raise TypeError(f"the reason is a string, not {type(value).__name__}")
        self._reason = None if value is None else _cut(value)


def _ending(exc: BaseException) -> tuple[Outcome, str | None, int | None]:
    """The outcome, reason and status of an action that raised `exc`."""
    if isinstance(exc, asyncio.CancelledError):
        return "failure", "cancelled", None
    if isinstance(exc, HTTPException):  # FastAPI's and Problem are Starlette's
        status = exc.status_code
        if status < 400:  # a redirection raised
            return "success", None, status
        return ("denied" if status in (401, 403) else "failure"), _type_name(exc), status
    if isinstance(exc, WebSocketException):
        denied = exc.code == WS_1008_POLICY_VIOLATION
        return ("denied" if denied else "failure"), _type_name(exc), None
    if isinstance(exc, RequestValidationError | WebSocketRequestValidationError):
        return "failure", _type_name(exc), 422 if isinstance(exc, RequestValidationError) else None
    return "failure", _type_name(exc), None


def _resource_fields(template: str) -> list[str]:
    """The path parameters a resource template uses; only plain `{name}` fields are allowed."""
    names = []
    for _, name, spec, conversion in string.Formatter().parse(template):
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise ValueError(
                f"invalid resource template {template!r}: its fields are path parameters, "
                "such as {invoice_id}, without attributes, indexes or formats"
            )
        names.append(name)
    return names


class AuditSink(Protocol):
    """Where events go. `write` gets each one, redacted and signed, when its action ends, within
    the FastAPIAudit's `timeout`: queue it if writing takes longer. What it raises is logged."""

    async def write(self, event: AuditEvent) -> None: ...


def _outputs(log: logging.Logger, level: int) -> bool:
    """Whether a record of `level` on `log` reaches a handler that writes it."""
    if not log.isEnabledFor(level):
        return False
    current: logging.Logger | None = log
    while current is not None:
        for handler in current.handlers:
            if not isinstance(handler, logging.NullHandler) and level >= handler.level:
                return True
        if not current.propagate:
            return False
        current = current.parent
    last_resort = logging.lastResort  # what logging uses when there's no handler at all
    return last_resort is not None and level >= last_resort.level


class LoggingAuditSink:
    """Writes each event as one line of JSON to the logger `log` (a name or a Logger), at
    `level`; handlers that format their own JSON find the event in the record's `audit` attribute
    too. When the logger outputs nothing at that level (no handler, or a higher level), it says so
    once, as a warning."""

    def __init__(
        self,
        log: str | logging.Logger = "fastapi_alcazaba.audit.events",
        level: int = logging.INFO,
    ) -> None:
        self.logger = log if isinstance(log, logging.Logger) else logging.getLogger(log)
        self.level = level
        self._warned = False

    async def write(self, event: AuditEvent) -> None:
        if not self._warned and not _outputs(self.logger, self.level):
            self._warned = True
            logger.warning(
                "Audit events are being lost: the logger %r outputs nothing at level %s. "
                "Give it a handler, or the root logger a level that lets them through",
                self.logger.name,
                logging.getLevelName(self.level),
            )
        self.logger.log(self.level, "%s", event.to_json(), extra={"audit": event.to_dict()})


class MemoryAuditSink:
    """Keeps the last `maxlen` events in `events`: for tests, and to look at them while
    developing."""

    def __init__(self, maxlen: int | None = 1000) -> None:
        self.events: deque[AuditEvent] = deque(maxlen=maxlen)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class FastAPIAudit:
    """An audit trail: `action()` records what endpoints do, `record()` and `log()` the rest.

    :param sinks: where the events go; a LoggingAuditSink by default.
    :param signing_key: signs and chains the events (see `verify_audit_trail`); a secret of at
        least 32 bytes, or several to rotate it (the first signs). The same in every process.
    :param redact: words that mark secrets in the keys of `details`, besides `SENSITIVE_WORDS`.
    :param timeout: seconds each sink has to write an event.
    :param metrics: where to count the events (`fastapi_alcazaba.audit`) and those not written
        (`fastapi_alcazaba.audit.undelivered`); FastAPICache's by default.
    """

    def __init__(
        self,
        sinks: Iterable[AuditSink] | None = None,
        *,
        signing_key: Secrets | None = None,
        redact: Collection[str] = (),
        timeout: float = 5.0,
        metrics: Metrics | None = None,
    ) -> None:
        self.sinks: list[AuditSink] = [LoggingAuditSink()] if sinks is None else list(sinks)
        if not self.sinks:
            raise ValueError("give at least one sink")
        if not (math.isfinite(timeout) and timeout > 0):
            raise ValueError("timeout must be a positive number of seconds")
        if isinstance(redact, str):
            raise TypeError("redact is a list of words, not a single string")
        extra = {word.lower() for word in redact}
        if any(not word.isalnum() for word in extra):
            raise ValueError("the words of redact are letters and digits, such as 'iban'")
        self.timeout = timeout
        self._keys = secret_list(signing_key) if signing_key is not None else None
        self._cleaner = _Cleaner(SENSITIVE_WORDS | extra)
        self._metrics = metrics
        # The stream: a new one in each process, should FastAPIAudit be created before a fork.
        self._pid = 0
        self._stream = ""
        self._sequence = 0
        self._previous: str | None = None
        self._writing: set[asyncio.Task[None]] = set()

    def action(
        self,
        name: str,
        *,
        resource: str | None = None,
    ) -> Callable[[HTTPConnection], AsyncIterator[AuditEntry]]:
        """A dependency that records the action `name` when the endpoint ends, and gives the
        endpoint its AuditEntry to fill in (`actor`, `details`...). `resource` is a template of
        the path parameters, such as "invoice:{invoice_id}", filled in before anything runs: the
        requests refused are recorded with it too."""
        _check_action(name)
        fields = _resource_fields(resource) if resource is not None else []

        async def audited(connection: HTTPConnection) -> AsyncIterator[AuditEntry]:
            entry = AuditEntry(name)
            if resource is not None:
                params = connection.path_params
                missing = [field_name for field_name in fields if field_name not in params]
                if missing:
                    raise RuntimeError(
                        f"the audit resource {resource!r} of {name!r} needs the path "
                        f"parameters {missing}, which this route doesn't have"
                    )
                entry.resource = resource.format_map(params)
            async with self._recording(entry, connection):
                yield entry

        return audited

    @asynccontextmanager
    async def record(
        self,
        action: str,
        *,
        actor: str | int | uuid.UUID | None = None,
        resource: str | int | uuid.UUID | None = None,
        details: Mapping[str, Any] | None = None,
        request: HTTPConnection | None = None,
    ) -> AsyncIterator[AuditEntry]:
        """Records the block as the action `action`, when it ends: outside endpoints (jobs,
        tasks, commands), or around a part of one. `request` adds the request's method, route
        and client."""
        entry = AuditEntry(action, actor=actor, resource=resource, details=details)
        async with self._recording(entry, request):
            yield entry

    async def log(
        self,
        action: str,
        *,
        outcome: Outcome = "success",
        actor: str | int | uuid.UUID | None = None,
        resource: str | int | uuid.UUID | None = None,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        request: HTTPConnection | None = None,
    ) -> AuditEvent:
        """Writes one event and returns it, e.g. for a failed login. `request` adds the
        request's method, route and client."""
        entry = AuditEntry(action, actor=actor, resource=resource, details=details)
        entry.outcome = outcome
        entry.reason = reason
        return await self._write(entry, request, outcome, entry.reason, None)

    @asynccontextmanager
    async def _recording(
        self,
        entry: AuditEntry,
        connection: HTTPConnection | None,
    ) -> AsyncIterator[None]:
        try:
            yield
        except GeneratorExit:
            raise  # closed without ending: there's no outcome to tell
        except BaseException as exc:
            outcome, reason, status = _ending(exc)
            await self._write(entry, connection, outcome, entry.reason or reason, status)
            raise
        await self._write(entry, connection, entry.outcome or "success", entry.reason, None)

    async def _write(
        self,
        entry: AuditEntry,
        connection: HTTPConnection | None,
        outcome: Outcome,
        reason: str | None,
        status: int | None,
    ) -> AuditEvent:
        event = self._event(entry, connection, outcome, reason, status)
        # In a task of its own, shielded: a request cancelled meanwhile (its client gone, its
        # deadline passed) still gets its event written.
        task = asyncio.ensure_future(self._deliver(event))
        self._writing.add(task)
        task.add_done_callback(self._writing.discard)
        await asyncio.shield(task)
        return event

    def _event(
        self,
        entry: AuditEntry,
        connection: HTTPConnection | None,
        outcome: Outcome,
        reason: str | None,
        status: int | None,
    ) -> AuditEvent:
        method = route = client = None
        if connection is not None:
            scope = connection.scope
            method = scope.get("method")
            template = getattr(scope.get("route"), "path_format", None)
            if isinstance(template, str):
                route = f"{scope.get('root_path', '')}{template}"
            client = connection.client.host if connection.client else None
        parent = traceparent()
        if os.getpid() != self._pid:
            self._pid, self._stream = os.getpid(), secrets.token_hex(8)
            self._sequence, self._previous = 0, None
        self._sequence += 1
        event = AuditEvent(
            id=uuid.uuid4().hex,
            time=datetime.now(UTC),
            action=entry.action,
            outcome=outcome,
            actor=entry.actor,
            resource=entry.resource,
            reason=reason,
            status=status,
            request_id=request_id(),
            trace_id=parent.split("-")[1] if parent else None,
            method=method,
            route=route,
            client=client,
            details=self._cleaner.details(entry.details),
            stream=self._stream,
            sequence=self._sequence,
            previous=self._previous,  # only set with a signing key
        )
        if self._keys is None:
            return event
        data = event.to_dict()
        del data["signature"]
        event = dataclasses.replace(event, signature=_mac(self._keys[0], data))
        self._previous = event.signature
        return event

    async def _deliver(self, event: AuditEvent) -> None:
        metrics = self._metrics_in_use()
        metrics.count("audit", action=event.action, outcome=event.outcome)
        for sink in self.sinks:
            name = type(sink).__name__
            deadline = asyncio.timeout(self.timeout)
            try:
                async with deadline:
                    await sink.write(event)
            except Exception as exc:  # a sink must not keep the event from the others
                timed_out = isinstance(exc, TimeoutError) and deadline.expired()
                # The event goes in the message: logged, it isn't lost.
                logger.error(
                    "Audit event not written by %s (%s): %s",
                    name,
                    f"took longer than {self.timeout} s" if timed_out else _type_name(exc),
                    event.to_json(),
                    exc_info=not timed_out,
                )
                reason = "timeout" if timed_out else "error"
                metrics.count("audit.undelivered", sink=name, reason=reason)

    def _metrics_in_use(self) -> Metrics:
        if self._metrics is not None:
            return self._metrics
        from fastapi_alcazaba.response_cache.caches import FastAPICache

        return FastAPICache.get_metrics()


def verify_audit_trail(
    events: Iterable[AuditEvent | Mapping[str, Any]],
    signing_key: Secrets,
) -> list[str]:
    """What's wrong with a trail written with `signing_key` (or one of its keys, when it was
    rotated): an empty list when nothing is. `events` are AuditEvents or their `to_dict()` (the
    JSON lines read back), of one stream or several, in any order; the same event given twice
    counts once. Each stream's chain is checked from its oldest event given, since older ones may
    have been rotated out of the logs."""
    keys = secret_list(signing_key)
    problems: list[str] = []
    streams: dict[str, dict[str, dict[str, Any]]] = {}  # stream -> signature -> event
    for item in events:
        data = item.to_dict() if isinstance(item, AuditEvent) else dict(item)
        signature = data.pop("signature", None)
        if not isinstance(signature, str):
            problems.append(f"event {data.get('id')!r} isn't signed")
            continue
        if not any(_signed_by(key, data, signature.encode()) for key in keys):
            problems.append(
                f"event {data.get('id')!r} doesn't match its signature: altered, or not "
                "written with this key"
            )
            continue
        data["signature"] = signature
        streams.setdefault(data["stream"], {})[signature] = data
    for stream, signed in sorted(streams.items()):
        chain = sorted(signed.values(), key=lambda data: int(data["sequence"]))
        for before, after in zip(chain, chain[1:], strict=False):
            first, second = before["sequence"], after["sequence"]
            if second == first:
                problems.append(f"stream {stream}: two different events numbered {second}")
            elif second != first + 1:
                problems.append(f"stream {stream}: events {first + 1} to {second - 1} are missing")
            elif after["previous"] != before["signature"]:
                problems.append(f"stream {stream}: event {second} doesn't follow event {first}")
    return problems
