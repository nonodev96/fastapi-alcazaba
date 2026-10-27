# Sessions, CSRF and security headers

What a cookie-based app needs: server-side sessions, CSRF protection, headers and body limits.

In the guide: [Server-side sessions](../index.md#server-side-sessions) · [CSRF protection](../index.md#csrf-protection) · [Security headers and body size](../index.md#security-headers-and-body-size). In an app: [the example](../examples/browser_app.md).

::: fastapi_alcazaba.sessions.ServerSessionMiddleware

::: fastapi_alcazaba.sessions.rotate_session

::: fastapi_alcazaba.sessions.clear_session

::: fastapi_alcazaba.sessions.ServerSession

::: fastapi_alcazaba.csrf.CSRFMiddleware

::: fastapi_alcazaba.middleware.SecurityHeadersMiddleware

::: fastapi_alcazaba.middleware.BodySizeLimitMiddleware

::: fastapi_alcazaba.middleware.DEFAULT_SECURITY_HEADERS
