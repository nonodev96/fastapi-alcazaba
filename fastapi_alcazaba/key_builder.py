import hashlib
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import Response


def default_key_builder(
    func: Callable[..., Any],
    namespace: str = "",
    *,
    request: Request | None = None,
    response: Response | None = None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """`namespace` plus the SHA-256 of the function's module, qualified name and the repr() of
    its arguments.

    SHA-256, not MD5, and `__qualname__`, not `__name__`, so that two methods with the same name
    in one module don't share their entries. Request headers that change
    the response, such as Authorization, are added by the decorator (`vary_headers`), whatever the
    key builder.
    """
    raw = f"{func.__module__}:{func.__qualname__}:{args!r}:{kwargs!r}"
    return f"{namespace}:{hashlib.sha256(raw.encode()).hexdigest()}"
