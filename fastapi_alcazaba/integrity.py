"""Protected cache entries: signed (HMAC-SHA256) or encrypted (AES-256-GCM, see encryption.py),
always bound to their cache key."""

import hashlib
import hmac
from collections.abc import Sequence
from typing import Protocol

from pydantic import SecretBytes, SecretStr

_MAGIC = b"fastapi_alcazaba/s1"
_TAG_SIZE = hashlib.sha256().digest_size
MIN_KEY_SIZE = 32

#: A secret: text, bytes, or pydantic's SecretStr or SecretBytes, as pydantic-settings loads it.
Secret = str | bytes | SecretStr | SecretBytes
#: A secret, or several to rotate it: the first is used, the others still accepted.
Secrets = Secret | Sequence[Secret]


def reveal(secret: Secret) -> str | bytes:
    """The value of a secret, taken out of SecretStr or SecretBytes."""
    return secret.get_secret_value() if isinstance(secret, SecretStr | SecretBytes) else secret


class EntryProtector(Protocol):
    """Wraps what the cache stores and gives back only what it wrapped itself, for that key."""

    def seal(self, cache_key: str, payload: bytes) -> bytes: ...

    def open(self, cache_key: str, data: bytes) -> bytes | None:
        """The payload, or None when `data` wasn't sealed by us for `cache_key`."""
        ...


def secret_list(keys: Secrets) -> list[bytes]:
    """The secrets as bytes, newest first; each at least MIN_KEY_SIZE bytes long."""
    if isinstance(keys, str | bytes | SecretStr | SecretBytes):
        keys = [keys]
    if not keys:
        raise ValueError("at least one key is needed")
    values = [reveal(key) for key in keys]
    secrets = [value.encode() if isinstance(value, str) else value for value in values]
    if any(len(secret) < MIN_KEY_SIZE for secret in secrets):
        raise ValueError(
            f"keys must be at least {MIN_KEY_SIZE} bytes long; "
            "generate one with secrets.token_urlsafe(32)"
        )
    return secrets


class Signer:
    """Signs what the cache stores and only accepts what it signed itself.

    Whoever can write to the backend (a shared Redis, a leaked password) can't plant data, nor a
    pickle that runs code when it's read, nor copy one user's entry under another user's key: the
    tag covers the key too. The first key signs; the others are only checked, so a key can be
    rotated without losing the cache: put the new one first and drop the old one later.
    """

    def __init__(self, keys: Secrets) -> None:
        self._keys = secret_list(keys)

    @staticmethod
    def _tag(secret: bytes, cache_key: str, payload: bytes) -> bytes:
        mac = hmac.new(secret, digestmod=hashlib.sha256)
        mac.update(cache_key.encode())
        mac.update(b"\0")
        mac.update(payload)
        return mac.digest()

    def sign(self, cache_key: str, payload: bytes) -> bytes:
        return _MAGIC + self._tag(self._keys[0], cache_key, payload) + payload

    def unsign(self, cache_key: str, data: bytes) -> bytes | None:
        """The payload, or None when `data` wasn't signed by us for this key."""
        header = len(_MAGIC) + _TAG_SIZE
        if len(data) < header or not data.startswith(_MAGIC):
            return None
        tag, payload = data[len(_MAGIC) : header], data[header:]
        for secret in self._keys:
            if hmac.compare_digest(tag, self._tag(secret, cache_key, payload)):
                return payload
        return None

    seal = sign
    open = unsign
