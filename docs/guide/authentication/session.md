# SessionStrategy

The user kept in the session at login, for the browsers of an app's users.

In an app: [the example](../../examples/auth_strategies/session.md). How strategies are tried, and the code of a login that starts a session: [Strategies](index.md#strategies).
The sessions themselves: [Sessions, CSRF and security headers](../browser.md). Every argument:
[the API reference](../../reference/authentication.md#fastapi_alcazaba.auth.SessionStrategy).

`SessionStrategy` finds the user id that `sessions.login(request, user_id)` kept in the session,
and loads the user with `load_user`. With `ServerSessionMiddleware` the session gets a new id
at login (no session fixation), and `sessions.logout(request)` deletes it from the backend. A
session whose user doesn't exist any more fails.
