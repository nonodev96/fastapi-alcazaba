"""What the strategies put in the tokens they hand to clients: sort keys that keep their types
(datetimes, UUIDs, decimals...), packed as base64 JSON, and signed with HMAC-SHA256 when there's
a secret, so that a token a client made or changed is refused."""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from uuid import UUID

from fastapi_alcazaba.core.integrity import Secrets, secret_list

#: A value of an item's sort key that a token can hold.
CursorValue = str | int | float | bool | datetime | date | UUID | Decimal
#: Where a token points: the sort key of an item, one value or a tuple of them.
Position = CursorValue | tuple[CursorValue, ...]

MAX_TOKEN = 1024  # characters of a token
SEALED_PATTERN = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)?$"
MAX_VALUES = 8  # values in a position
MAX_INT = 2**63  # integers as databases sort them: larger ones from a client would overflow


def _decimal(text: str) -> Decimal:
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{text!r} isn't a number") from None
    if not number.is_finite():
        raise ValueError("a token can't hold NaN or infinity")
    return number


# Types that JSON doesn't have travel tagged, and come back as themselves.
_TAGGED: dict[str, Callable[[str], CursorValue]] = {
    "datetime": datetime.fromisoformat,
    "date": date.fromisoformat,
    "uuid": UUID,
    "decimal": _decimal,
}


def encode_value(value: object) -> object:
    """`value` as JSON: as itself, or tagged with its type."""
    if isinstance(value, int) and not isinstance(value, bool) and not -MAX_INT <= value < MAX_INT:
        raise ValueError("a token holds integers of up to 64 bits")
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float | Decimal) and not isfinite(value):
        raise ValueError("a token can't hold NaN or infinity")
    if isinstance(value, float):
        return value
    for tag, kind in (
        ("datetime", datetime),
        ("date", date),
        ("uuid", UUID),
        ("decimal", Decimal),
    ):
        if isinstance(value, kind):
            text = value.isoformat() if isinstance(value, date) else str(value)
            return {"t": tag, "v": text}
    raise TypeError(f"a token can't hold a {type(value).__name__}: give the sort key as values")


def decode_value(value: object) -> CursorValue:
    """The value that `encode_value` gave `value` for. Raises ValueError for anything else."""
    if isinstance(value, int) and not isinstance(value, bool) and not -MAX_INT <= value < MAX_INT:
        raise ValueError("an integer of more than 64 bits")
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict) and value.keys() == {"t", "v"}:
        tag, text = value["t"], value["v"]
        if isinstance(tag, str) and isinstance(text, str) and tag in _TAGGED:
            return _TAGGED[tag](text)
    raise ValueError("not a value of a sort key")


def encode_position(position: Position) -> object:
    """`position` as JSON: a value, or a list of them."""
    if isinstance(position, tuple):
        if not 1 <= len(position) <= MAX_VALUES:
            raise ValueError(f"a position has 1 to {MAX_VALUES} values")
        return [encode_value(value) for value in position]
    return encode_value(position)


def decode_position(decoded: object) -> Position:
    """The position that `encode_position` gave `decoded` for. Raises ValueError otherwise."""
    if isinstance(decoded, list):
        if not 1 <= len(decoded) <= MAX_VALUES:
            raise ValueError("too many values")
        return tuple(decode_value(value) for value in decoded)
    return decode_value(decoded)


def _no_constants(name: str) -> object:
    raise ValueError(f"{name} isn't a value of a sort key")


class Sealer:
    """Packs JSON into tokens (base64), signed with a key of its own when there's a secret.

    :param secret: at least 32 bytes; a list rotates them, the first one signing and all of them
        accepted. None for unsigned tokens.
    :param purpose: what the tokens are for: a key of their own is derived for it, so that the
        tokens of one strategy are never taken by another, nor the secret's other signatures.
    """

    def __init__(
        self,
        secret: Secrets | None,
        purpose: bytes,
    ) -> None:
        self._keys = (
            [hmac.new(key, purpose, hashlib.sha256).digest() for key in secret_list(secret)]
            if secret is not None
            else None
        )

    @property
    def signs(self) -> bool:
        return self._keys is not None

    def seal(
        self,
        value: object,
    ) -> str:
        """The token of `value` (JSON-serialisable)."""
        text = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        payload = base64.urlsafe_b64encode(text).rstrip(b"=").decode()
        if self._keys is None:
            return payload
        return f"{payload}.{self._tag(self._keys[0], payload)}"

    def open(
        self,
        token: str,
    ) -> object:
        """What `token` holds. Raises ValueError if it isn't one of these tokens."""
        # Tokens are ASCII (base64), and compare_digest refuses strings that aren't.
        if len(token) > MAX_TOKEN or not token.isascii():
            raise ValueError("not a token")
        payload, _, tag = token.partition(".")
        if self._keys is not None and not any(
            hmac.compare_digest(tag, self._tag(key, payload)) for key in self._keys
        ):
            raise ValueError("a token that isn't ours")
        try:
            text = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            return json.loads(text, parse_constant=_no_constants)
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("not a token") from None

    @staticmethod
    def _tag(
        key: bytes,
        payload: str,
    ) -> str:
        digest = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
