"""Cookies for the middlewares that set them (server sessions, CSRF tokens): reading them from the
request and writing `Set-Cookie` headers with secure attributes, checked once at start-up."""

import re
from dataclasses import dataclass
from typing import Literal

from starlette.types import Scope

SameSite = Literal["lax", "strict", "none"]

_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_PATH = re.compile(r"/[\x21-\x3a\x3c-\x7e]*")  # printable ASCII without ";"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_DOMAIN = re.compile(rf"\.?{_LABEL}(?:\.{_LABEL})*")
_EXPIRED = "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"


@dataclass(frozen=True, slots=True)
class CookieSettings:
    """A cookie's name and attributes. Refuses combinations browsers would drop: `SameSite=None`
    without `Secure`, and the `__Host-` and `__Secure-` prefixes without what they require."""

    name: str
    secure: bool = True
    same_site: SameSite = "lax"
    http_only: bool = True
    path: str = "/"
    domain: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"invalid cookie name: {self.name!r}")
        if self.same_site not in ("lax", "strict", "none"):
            raise ValueError(f"same_site is lax, strict or none, not {self.same_site!r}")
        if self.same_site == "none" and not self.secure:
            raise ValueError("browsers drop SameSite=None cookies that aren't Secure")
        if not _PATH.fullmatch(self.path):
            raise ValueError(f"invalid cookie path: {self.path!r}")
        if self.domain is not None and not _DOMAIN.fullmatch(self.domain):
            raise ValueError(f"invalid cookie domain: {self.domain!r}")
        prefix = self.name.lower()
        if prefix.startswith("__secure-") and not self.secure:
            raise ValueError(f"{self.name} cookies must be Secure: pick another name")
        if prefix.startswith("__host-") and (
            not self.secure or self.path != "/" or self.domain is not None
        ):
            raise ValueError(
                f"{self.name} cookies must be Secure, with path / and no domain: pick another name"
            )

    def _attributes(self) -> str:
        parts = [f"Path={self.path}"]
        if self.domain is not None:
            parts.append(f"Domain={self.domain}")
        if self.http_only:
            parts.append("HttpOnly")
        if self.secure:
            parts.append("Secure")
        parts.append(f"SameSite={self.same_site.capitalize()}")
        return "; ".join(parts)

    def header(self, value: str, max_age: int | None = None) -> tuple[bytes, bytes]:
        """The `Set-Cookie` header that stores `value` (a session cookie without `max_age`)."""
        lifetime = "" if max_age is None else f"; Max-Age={max_age}"
        text = f"{self.name}={value}{lifetime}; {self._attributes()}"
        return b"set-cookie", text.encode("latin-1")

    def expired(self) -> tuple[bytes, bytes]:
        """The `Set-Cookie` header that makes the browser forget the cookie."""
        return b"set-cookie", f"{self.name}=; {_EXPIRED}; {self._attributes()}".encode("latin-1")


def cookie_values(scope: Scope, name: str) -> list[str]:
    """Every value the request sends for the cookie `name`, in order: a sibling subdomain can add
    one of its own (cookie tossing), which `__Host-` names prevent."""
    values = []
    for header, raw in scope["headers"]:
        if header != b"cookie":
            continue
        for pair in raw.decode("latin-1").split(";"):
            key, sep, value = pair.partition("=")
            if sep and key.strip() == name:
                values.append(value.strip())
    return values


def has_cookies(scope: Scope) -> bool:
    """Whether the request carries any cookie."""
    return any(header == b"cookie" and raw.strip() for header, raw in scope["headers"])
