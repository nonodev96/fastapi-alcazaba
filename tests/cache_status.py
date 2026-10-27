"""What Alcazaba's member of a response's Cache-Status header (RFC 9211) says."""

from collections.abc import Mapping


def cache_status(headers: Mapping[str, str], name: str = "Alcazaba") -> str | None:
    """The parameters of the cache `name` in Cache-Status ("hit; ttl=60", "fwd=miss; stored");
    None when it isn't there."""
    for member in headers.get("cache-status", "").split(","):
        member_name, _, parameters = member.strip().partition(";")
        if member_name == name:
            return parameters.strip()
    return None


def served(headers: Mapping[str, str]) -> str | None:
    """Whether the cache answered, as "hit", or else why the request reached the endpoint
    ("miss", "stale", "request", "method", "bypass"); None without the header."""
    parameters = cache_status(headers)
    if parameters is None:
        return None
    for parameter in parameters.split(";"):
        key, _, value = parameter.strip().partition("=")
        if key == "hit":
            return "hit"
        if key == "fwd":
            return value
    raise AssertionError(f"a Cache-Status member without hit nor fwd: {parameters!r}")
