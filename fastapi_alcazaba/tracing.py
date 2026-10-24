"""OpenTelemetry traces when opentelemetry-api is installed (the `otel` extra); no-ops otherwise.

Spans:
- "fastapi_alcazaba <function>" around each cached call [fastapi_alcazaba.function,
  fastapi_alcazaba.namespace, fastapi_alcazaba.result: hit, miss, refresh, bypass].
- "fastapi_alcazaba.backend <operation>" around each backend call, a child of the former
  [fastapi_alcazaba.operation: get, set, lock, unlock, tag]; failures are recorded on it.

Instrumented clients (redis, valkey) nest their own spans inside these.
"""

from contextlib import AbstractContextManager, nullcontext
from typing import Any

try:
    from opentelemetry import trace as otel_trace
except ImportError:  # pragma: no cover  # the otel extra isn't installed
    otel_trace = None  # type: ignore[assignment]


class Tracer:
    def __init__(
        self, enabled: bool = True, tracer_provider: Any = None, version: str = ""
    ) -> None:
        self.enabled = enabled and otel_trace is not None
        if self.enabled:
            self._tracer = otel_trace.get_tracer(
                "fastapi_alcazaba", version, tracer_provider=tracer_provider
            )

    def call(self, function: str, namespace: str) -> AbstractContextManager[Any]:
        """Span of a cached call; yields it (None when tracing is off) to set the result."""
        if not self.enabled:
            return nullcontext(None)
        return self._tracer.start_as_current_span(
            f"fastapi_alcazaba {function}",
            kind=otel_trace.SpanKind.INTERNAL,
            attributes={
                "fastapi_alcazaba.function": function,
                "fastapi_alcazaba.namespace": namespace,
            },
        )

    def backend(self, operation: str) -> AbstractContextManager[Any]:
        if not self.enabled:
            return nullcontext(None)
        return self._tracer.start_as_current_span(
            f"fastapi_alcazaba.backend {operation}",
            kind=otel_trace.SpanKind.CLIENT,
            attributes={"fastapi_alcazaba.operation": operation},
        )
