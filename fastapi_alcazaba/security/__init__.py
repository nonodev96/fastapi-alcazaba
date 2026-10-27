"""What an app that browsers talk to needs: server-side sessions, CSRF protection, security
headers and a limit on the size of request bodies."""

from fastapi_alcazaba.security.body_size import BodySizeLimitMiddleware
from fastapi_alcazaba.security.csrf import CSRFMiddleware
from fastapi_alcazaba.security.headers import DEFAULT_SECURITY_HEADERS, SecurityHeadersMiddleware
from fastapi_alcazaba.security.sessions import (
    ServerSession,
    ServerSessionMiddleware,
    clear_session,
    rotate_session,
)

__all__ = [
    "BodySizeLimitMiddleware",
    "clear_session",
    "CSRFMiddleware",
    "DEFAULT_SECURITY_HEADERS",
    "rotate_session",
    "SecurityHeadersMiddleware",
    "ServerSession",
    "ServerSessionMiddleware",
]
