"""Errors and observability: problem details (RFC 9457), request IDs, request deadlines, health
checks and structured logs (JSON)."""

from fastapi_alcazaba.observability.backlog import BacklogMonitor
from fastapi_alcazaba.observability.health import (
    HealthCheck,
    broadcast_check,
    health_router,
    leader_check,
    object_storage_check,
    smtp_check,
    task_queue_check,
)
from fastapi_alcazaba.observability.logs import JSONFormatter, logging_config
from fastapi_alcazaba.observability.problems import FastAPIProblemDetails, Problem
from fastapi_alcazaba.observability.request_id import (
    RequestIDLogFilter,
    RequestIDMiddleware,
    request_id,
)
from fastapi_alcazaba.observability.timeouts import RequestTimeoutMiddleware

__all__ = [
    "BacklogMonitor",
    "broadcast_check",
    "FastAPIProblemDetails",
    "health_router",
    "HealthCheck",
    "JSONFormatter",
    "leader_check",
    "logging_config",
    "object_storage_check",
    "Problem",
    "request_id",
    "RequestIDLogFilter",
    "RequestIDMiddleware",
    "RequestTimeoutMiddleware",
    "smtp_check",
    "task_queue_check",
]
