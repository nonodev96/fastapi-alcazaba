"""OpenTelemetry metrics when opentelemetry-api is installed (the `otel` extra); no-ops otherwise.

Instruments (attributes in brackets):
- fastapi_alcazaba.lookups: requests by result [fastapi_alcazaba.result: hit, miss, refresh,
  bypass; fastapi_alcazaba.namespace]
- fastapi_alcazaba.stores: what happened to computed values [fastapi_alcazaba.outcome: stored,
  uncacheable, too_large, unencodable, failed; fastapi_alcazaba.namespace]
- fastapi_alcazaba.entry.size: bytes of stored entries [fastapi_alcazaba.namespace]
- fastapi_alcazaba.backend.duration: seconds per backend call [fastapi_alcazaba.operation]
- fastapi_alcazaba.backend.errors: failed backend calls [fastapi_alcazaba.operation, error.type]
- fastapi_alcazaba.rate_limit: requests seen by a FastAPIRateLimiter [fastapi_alcazaba.outcome:
  allowed, limited, unchecked (the backend failed); fastapi_alcazaba.limiter: its name or route]
- fastapi_alcazaba.breaker.calls: calls through a FastAPIBreaker [fastapi_alcazaba.outcome:
  success, failure, refused; fastapi_alcazaba.breaker: its name]
- fastapi_alcazaba.breaker.transitions: a FastAPIBreaker opening or closing again
  [fastapi_alcazaba.state: open, closed; fastapi_alcazaba.breaker]
- fastapi_alcazaba.scheduler.runs: ticks of a FastAPIScheduler job [fastapi_alcazaba.outcome:
  ran, failed, skipped (another process has it), overlapping (its last run is still going),
  unchecked (the backend failed); fastapi_alcazaba.job: its name]

And those of `COUNTERS` and `HISTOGRAMS`, which the other tools record with `count()` and
`record()`.
"""

from typing import Any

#: Counters recorded with `Metrics.count()`: name (after "fastapi_alcazaba.") -> unit, description.
COUNTERS: dict[str, tuple[str, str]] = {
    "idempotency": (
        "{request}",
        "Requests with an Idempotency-Key, by outcome: stored, replayed, conflict, mismatch, "
        "not_kept, invalid, too_large, unchecked",
    ),
    "tokens.checks": (
        "{check}",
        "Tokens checked against the blocklist, by outcome: valid, revoked, unchecked",
    ),
    "otp.verifications": (
        "{attempt}",
        "One-time codes verified, by outcome: success, wrong, missing (expired or used), "
        "exhausted (too many guesses), malformed, unchecked",
    ),
    "lock.lost": ("{lock}", "Locks lost while held: their renewals couldn't keep up"),
    "health.checks": ("{check}", "Readiness checks, by check and status: ok, fail"),
    "concurrency": (
        "{request}",
        "Slots asked of a FastAPIConcurrencyLimiter, by outcome (acquired, local: counted in "
        "the process while the backend failed, refused, unavailable) and limiter",
    ),
    "leader.transitions": (
        "{transition}",
        "Leaderships of FastAPILeader, by leader and event: elected, lost, resigned",
    ),
    "maintenance.refused": ("{request}", "Requests refused by MaintenanceMiddleware"),
    "flags.evaluations": (
        "{evaluation}",
        "Feature flags asked about, by flag and result: on, off, default (undefined or unread)",
    ),
    "problems": (
        "{response}",
        "Error responses of FastAPIProblemDetails, by status and kind: http_exception, problem, "
        "validation, unhandled",
    ),
    "request_timeouts": (
        "{request}",
        "Requests past their deadline, by outcome (504, cut: already streaming) and route",
    ),
    "request_ids": (
        "{request}",
        "Request IDs, by source: incoming, generated, rejected (an unsafe incoming one replaced)",
    ),
    "outbound.breaker": (
        "{event}",
        "Circuit breakers of outbound origins, by event (opened, refused) and server.address",
    ),
    "outbound.ssrf.blocked": (
        "{request}",
        "Outbound requests SSRFGuardTransport refused, by reason: scheme, host, port, address",
    ),
    "webhooks": (
        "{delivery}",
        "Webhooks received, by outcome: accepted, replayed, rejected (signature or timestamp), "
        "malformed, too_large, unchecked (backend down, accepted), unavailable (refused)",
    ),
    "api_keys": (
        "{request}",
        "API keys checked, by outcome: accepted, malformed, unknown, wrong_secret, expired, "
        "insufficient_scope",
    ),
    "csrf.refused": (
        "{request}",
        "Requests refused by CSRFMiddleware, by kind (http, websocket) and reason: fetch_site "
        "(Sec-Fetch-Site), origin (Origin or Referer), no_source (neither), token",
    ),
    "sessions": (
        "{operation}",
        "Server sessions, by event: created, updated, rotated, deleted, touched (idle timeout "
        "slid), rejected (not ours: forged or altered), unreadable (backend down, read as "
        "empty), unavailable (backend down, answered 503)",
    ),
    "uploads": (
        "{file}",
        "Uploads checked by FastAPIUploads, by outcome: accepted, too_large, too_many_pixels, "
        "unsupported, unreadable (an image without readable dimensions), empty",
    ),
    "tasks.enqueued": ("{task}", "Tasks enqueued in FastAPITaskQueue, by queue and task"),
    "tasks": (
        "{task}",
        "Runs of FastAPITaskQueue tasks, by queue, task and outcome: succeeded, retried, failed "
        "(to the dead letters), interrupted (stop() ran out of grace: back to the queue); and "
        "tasks refused, by reason: malformed, tampered, too_large, unknown_task, "
        "invalid_arguments, abandoned (task only when it's registered)",
    ),
    "tasks.reclaimed": (
        "{task}",
        "Tasks taken over from workers that stopped answering, by queue",
    ),
    "audit": ("{event}", "Audit events, by action and outcome: success, failure, denied"),
    "audit.undelivered": (
        "{event}",
        "Audit events a sink didn't write, by sink and reason: error, timeout",
    ),
}
#: Histograms recorded with `Metrics.record()`: name -> unit, description.
HISTOGRAMS: dict[str, tuple[str, str]] = {
    "lock.wait": ("s", "Seconds spent acquiring locks, by acquired: true, false"),
    "health.check.duration": ("s", "Duration of the readiness checks, by check"),
    "uploads.size": ("By", "Size of the uploads accepted, by content_type (the detected one)"),
    "tasks.duration": ("s", "Duration of task runs, by queue, task and outcome"),
}

try:
    from opentelemetry import metrics as otel_metrics
except ImportError:  # pragma: no cover  # the otel extra isn't installed
    otel_metrics = None  # type: ignore[assignment]


class Metrics:
    def __init__(self, enabled: bool = True, meter_provider: Any = None, version: str = "") -> None:
        self.enabled = enabled and otel_metrics is not None
        if not self.enabled:
            return
        meter = otel_metrics.get_meter("fastapi_alcazaba", version, meter_provider=meter_provider)
        self._lookups = meter.create_counter(
            "fastapi_alcazaba.lookups", unit="{request}", description="Cached calls by result"
        )
        self._stores = meter.create_counter(
            "fastapi_alcazaba.stores", unit="{value}", description="Computed values by outcome"
        )
        self._entry_size = meter.create_histogram(
            "fastapi_alcazaba.entry.size", unit="By", description="Size of the stored entries"
        )
        self._backend_duration = meter.create_histogram(
            "fastapi_alcazaba.backend.duration",
            unit="s",
            description="Duration of the backend calls",
        )
        self._backend_errors = meter.create_counter(
            "fastapi_alcazaba.backend.errors", unit="{error}", description="Failed backend calls"
        )
        self._rate_limit = meter.create_counter(
            "fastapi_alcazaba.rate_limit",
            unit="{request}",
            description="Requests seen by the rate limiters, by outcome",
        )
        self._breaker_calls = meter.create_counter(
            "fastapi_alcazaba.breaker.calls",
            unit="{call}",
            description="Calls through the circuit breakers, by outcome",
        )
        self._breaker_transitions = meter.create_counter(
            "fastapi_alcazaba.breaker.transitions",
            unit="{transition}",
            description="Circuit breakers opening or closing again",
        )
        self._scheduler_runs = meter.create_counter(
            "fastapi_alcazaba.scheduler.runs",
            unit="{tick}",
            description="Ticks of the scheduled jobs, by outcome",
        )
        self._meter = meter
        self._instruments: dict[str, Any] = {}

    def lookup(self, result: str, namespace: str) -> None:
        if self.enabled:
            self._lookups.add(
                1, {"fastapi_alcazaba.result": result, "fastapi_alcazaba.namespace": namespace}
            )

    def store(self, outcome: str, namespace: str, size: int | None = None) -> None:
        if self.enabled:
            attributes = {"fastapi_alcazaba.namespace": namespace}
            self._stores.add(1, {"fastapi_alcazaba.outcome": outcome, **attributes})
            if size is not None:
                self._entry_size.record(size, attributes)

    def backend_duration(self, operation: str, seconds: float) -> None:
        if self.enabled:
            self._backend_duration.record(seconds, {"fastapi_alcazaba.operation": operation})

    def backend_error(self, operation: str, error_type: str) -> None:
        if self.enabled:
            self._backend_errors.add(
                1, {"fastapi_alcazaba.operation": operation, "error.type": error_type}
            )

    def rate_limit(self, outcome: str, limiter: str) -> None:
        if self.enabled:
            self._rate_limit.add(
                1, {"fastapi_alcazaba.outcome": outcome, "fastapi_alcazaba.limiter": limiter}
            )

    def breaker_call(self, outcome: str, breaker: str) -> None:
        if self.enabled:
            self._breaker_calls.add(
                1, {"fastapi_alcazaba.outcome": outcome, "fastapi_alcazaba.breaker": breaker}
            )

    def breaker_transition(self, state: str, breaker: str) -> None:
        if self.enabled:
            self._breaker_transitions.add(
                1, {"fastapi_alcazaba.state": state, "fastapi_alcazaba.breaker": breaker}
            )

    def scheduler_run(self, outcome: str, job: str) -> None:
        if self.enabled:
            self._scheduler_runs.add(
                1, {"fastapi_alcazaba.outcome": outcome, "fastapi_alcazaba.job": job}
            )

    def count(self, name: str, **attributes: str) -> None:
        """Count one on the counter `fastapi_alcazaba.<name>` (one of COUNTERS). Attribute names
        get the `fastapi_alcazaba.` prefix, unless they're dotted already (`server.address`)."""
        if name not in COUNTERS:
            raise ValueError(f"unknown counter {name!r}")
        if self.enabled:
            self._instrument(name, COUNTERS[name], histogram=False).add(1, _attributes(attributes))

    def record(self, name: str, value: float, **attributes: str) -> None:
        """Record `value` on the histogram `fastapi_alcazaba.<name>` (one of HISTOGRAMS)."""
        if name not in HISTOGRAMS:
            raise ValueError(f"unknown histogram {name!r}")
        if self.enabled:
            instrument = self._instrument(name, HISTOGRAMS[name], histogram=True)
            instrument.record(value, _attributes(attributes))

    def _instrument(self, name: str, spec: tuple[str, str], *, histogram: bool) -> Any:
        instrument = self._instruments.get(name)
        if instrument is None:
            unit, description = spec
            create = self._meter.create_histogram if histogram else self._meter.create_counter
            instrument = create(f"fastapi_alcazaba.{name}", unit=unit, description=description)
            self._instruments[name] = instrument
        return instrument


def _attributes(attributes: dict[str, str]) -> dict[str, str]:
    return {
        (name if "." in name else f"fastapi_alcazaba.{name}"): value
        for name, value in attributes.items()
    }
