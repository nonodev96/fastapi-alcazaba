"""The user of the request's session: who logged in before, by another strategy.

    sessions = auth.use(SessionStrategy(load_user))
    GitHubUser = Annotated[User, Depends(auth.authenticate("github"))]

    @app.get("/auth/github/callback")
    async def callback(request: Request, user: GitHubUser) -> RedirectResponse:
        sessions.login(request, user.id)  # a new session id: no session fixation
        return RedirectResponse("/", status_code=303)

`sessions.login()` keeps the user id in the session, and `sessions.logout()` deletes the session.
"""

from collections.abc import Callable
from typing import Any

from starlette.requests import HTTPConnection

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, Strategy, call
from fastapi_alcazaba.security.sessions import ServerSession, clear_session, rotate_session


class SessionStrategy(Strategy):
    """Authenticates by the user id that `login` kept in the session, with `ServerSessionMiddleware`
    (or Starlette's `SessionMiddleware`).

    :param load_user: `load_user(user_id)`: the user, or None when there's none any more (then
        authentication fails). Async, or a plain function, which runs in a thread.
    :param key: where in the session the user id is kept.
    :param name: what `FastAPIAuth.authenticate` calls it by.
    """

    def __init__(
        self,
        load_user: Callable[[Any], Any],
        *,
        key: str = "user_id",
        name: str = "session",
    ) -> None:
        super().__init__(name)
        self.load_user = load_user
        self.key = key

    async def authenticate(self, connection: HTTPConnection) -> Any:
        user_id = _session(connection).get(self.key)
        if user_id is None:
            return None
        user = await call(self.load_user, user_id)
        if user is None:
            raise AuthenticationFailed("the session's user doesn't exist any more")
        return user

    def login(self, connection: HTTPConnection, user_id: Any) -> None:
        """Keep `user_id` (a JSON value) in the session: the next requests are that user's. With
        ServerSessionMiddleware the session gets a new id, so that one someone planted in the
        browser before (session fixation) doesn't log them in too."""
        session = _session(connection)
        if isinstance(session, ServerSession):
            rotate_session(connection)
        session[self.key] = user_id

    def logout(self, connection: HTTPConnection) -> None:
        """Empty the session; with ServerSessionMiddleware it's also deleted from the backend."""
        session = _session(connection)
        if isinstance(session, ServerSession):
            clear_session(connection)
        else:
            session.clear()


def _session(connection: HTTPConnection) -> dict[str, Any]:
    session: dict[str, Any] | None = connection.scope.get("session")
    if session is None:
        raise RuntimeError("SessionStrategy needs ServerSessionMiddleware (or SessionMiddleware)")
    return session
