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
"""

from typing import Any

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
