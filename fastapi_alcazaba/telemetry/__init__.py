"""OpenTelemetry metrics and traces of every tool (with the `otel` extra)."""

from fastapi_alcazaba.telemetry.metrics import Metrics
from fastapi_alcazaba.telemetry.tracing import Tracer

__all__ = [
    "Metrics",
    "Tracer",
]
