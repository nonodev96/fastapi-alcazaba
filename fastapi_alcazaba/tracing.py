"""OpenTelemetry traces when opentelemetry-api is installed (the `otel` extra); no-ops otherwise.

Spans:
- "fastapi_alcazaba <function>" around each cached call [fastapi_alcazaba.function,
  fastapi_alcazaba.namespace, fastapi_alcazaba.result: hit, miss, refresh, bypass].
- "fastapi_alcazaba.backend <operation>" around each backend call, a child of the former
  [fastapi_alcazaba.operation: get, set, lock, unlock, tag]; failures are recorded on it.

- "fastapi_alcazaba.scheduler <job>", the root of a trace of its own, around each run of a
  scheduled job [fastapi_alcazaba.job, fastapi_alcazaba.tick]; failures are recorded on it.
- "fastapi_alcazaba.task <task>", the root of a trace of its own linked to the span that enqueued
  the task, around each run of a FastAPITaskQueue task [fastapi_alcazaba.queue,
  fastapi_alcazaba.task, fastapi_alcazaba.task_id, fastapi_alcazaba.attempt]; failures are
  recorded on it.

Instrumented clients (redis, valkey) nest their own spans inside these. The other tools add events
and attributes to the current span (the request's, with FastAPI's instrumentation): `add_event`
and `set_attributes`.
"""

from contextlib import AbstractContextManager, nullcontext
from typing import Any

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
except ImportError:  # pragma: no cover  # the otel extra isn't installed
    otel_trace = None  # type: ignore[assignment]
    otel_context = None  # type: ignore[assignment]
    TraceContextTextMapPropagator = None  # type: ignore[assignment,misc]


def traceparent() -> str | None:
    """The W3C `traceparent` of the current span, for work done later (elsewhere) to link to it;
    None when there's no span."""
    if TraceContextTextMapPropagator is None:  # pragma: no cover  # the tests have the otel extra
        return None
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent")


def add_event(name: str, **attributes: str | int | float | bool) -> None:
    """An event on the current span, if one is being recorded."""
    if otel_trace is not None:  # pragma: no branch  # the tests have the otel extra
        span = otel_trace.get_current_span()
        if span.is_recording():
            span.add_event(name, attributes)


def record_failure(span: Any, exc: BaseException) -> None:
    """Record on `span` (None when tracing is off) an exception that was handled, not raised."""
    if span is not None:
        span.record_exception(exc)
        span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, type(exc).__name__))


def set_attributes(**attributes: str | int | float | bool) -> None:
    """Attributes of the current span, if one is being recorded; names get the
    `fastapi_alcazaba.` prefix."""
    if otel_trace is not None:  # pragma: no branch  # the tests have the otel extra
        span = otel_trace.get_current_span()
        if span.is_recording():
            span.set_attributes({f"fastapi_alcazaba.{k}": v for k, v in attributes.items()})


class Tracer:
    def __init__(
        self,
        enabled: bool = True,
        tracer_provider: Any = None,
        version: str = "",
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

    def job(self, job: str, tick: str) -> AbstractContextManager[Any]:
        """Span of a scheduled job's run: the root of a trace of its own, whatever was current
        when the scheduler started. Yields it (None when tracing is off) to record failures."""
        if not self.enabled:
            return nullcontext(None)
        return self._tracer.start_as_current_span(
            f"fastapi_alcazaba.scheduler {job}",
            context=otel_context.Context(),
            kind=otel_trace.SpanKind.INTERNAL,
            attributes={"fastapi_alcazaba.job": job, "fastapi_alcazaba.tick": tick},
        )

    def task(
        self,
        queue: str,
        task: str,
        task_id: str,
        attempt: int,
        origin: str | None,
    ) -> AbstractContextManager[Any]:
        """Span of a task's run: the root of a trace of its own (a task may run long after, and
        many times), linked to the span that enqueued it (`origin`, its traceparent). Yields it
        (None when tracing is off) to record failures."""
        if not self.enabled:
            return nullcontext(None)
        links = []
        if origin:
            carrier = {"traceparent": origin}
            enqueued = otel_trace.get_current_span(TraceContextTextMapPropagator().extract(carrier))
            if enqueued.get_span_context().is_valid:
                links.append(otel_trace.Link(enqueued.get_span_context()))
        return self._tracer.start_as_current_span(
            f"fastapi_alcazaba.task {task}",
            context=otel_context.Context(),
            kind=otel_trace.SpanKind.CONSUMER,
            links=links,
            attributes={
                "fastapi_alcazaba.queue": queue,
                "fastapi_alcazaba.task": task,
                "fastapi_alcazaba.task_id": task_id,
                "fastapi_alcazaba.attempt": attempt,
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
