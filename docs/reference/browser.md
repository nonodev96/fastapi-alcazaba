# Sessions, CSRF and security headers

What a cookie-based app needs: server-side sessions, CSRF protection, headers and body limits.
In `fastapi_alcazaba.security`.

In the guide: [Server-side sessions](../guide/browser.md#server-side-sessions) · [CSRF protection](../guide/browser.md#csrf-protection) · [Security headers and body size](../guide/browser.md#security-headers-and-body-size). In an app: [the example](../examples/browser_app.md).

::: fastapi_alcazaba.security.ServerSessionMiddleware

::: fastapi_alcazaba.security.rotate_session

::: fastapi_alcazaba.security.clear_session

::: fastapi_alcazaba.security.ServerSession

::: fastapi_alcazaba.security.CSRFMiddleware

::: fastapi_alcazaba.security.SecurityHeadersMiddleware

::: fastapi_alcazaba.security.BodySizeLimitMiddleware

::: fastapi_alcazaba.security.DEFAULT_SECURITY_HEADERS
