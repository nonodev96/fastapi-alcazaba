"""OpenTelemetry metrics when opentelemetry-api is installed (the `otel` extra); no-ops otherwise.

Instruments (attributes in brackets):
- fastapi_alcazaba.lookups: requests by result [fastapi_alcazaba.result: hit, miss, refresh,
  bypass; fastapi_alcazaba.namespace]
- fastapi_alcazaba.stores: what happened to computed values [fastapi_alcazaba.outcome: stored,
  uncacheable, too_large, unencodable, failed; fastapi_alcazaba.namespace]
- fastapi_alcazaba.entry.size: bytes of stored entries [fastapi_alcazaba.namespace]
- fastapi_alcazaba.backend.duration: seconds per backend call [fastapi_alcazaba.operation]
- fastapi_alcazaba.backend.errors: failed backend calls [fastapi_alcazaba.operation, error.type]
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
