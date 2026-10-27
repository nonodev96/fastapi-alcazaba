"""Errors and observability: problem details (RFC 9457), request IDs, request deadlines and
health checks."""

from fastapi_alcazaba.observability.health import HealthCheck, health_router
from fastapi_alcazaba.observability.problems import FastAPIProblemDetails, Problem
from fastapi_alcazaba.observability.request_id import (
    RequestIDLogFilter,
    RequestIDMiddleware,
    request_id,
)
from fastapi_alcazaba.observability.timeouts import RequestTimeoutMiddleware

__all__ = [
    "FastAPIProblemDetails",
    "health_router",
    "HealthCheck",
    "Problem",
    "request_id",
    "RequestIDLogFilter",
    "RequestIDMiddleware",
    "RequestTimeoutMiddleware",
]
