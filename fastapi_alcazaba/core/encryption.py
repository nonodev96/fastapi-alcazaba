"""Encrypted cache entries: AES-256-GCM, with the cache key as associated data.

Needs the `crypto` extra (cryptography).
"""

import hashlib
import os

from fastapi_alcazaba.core.integrity import Secrets, secret_list

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError:  # pragma: no cover  # the crypto extra isn't installed
    AESGCM = None  # type: ignore[assignment,misc]

_MAGIC = b"fastapi_alcazaba/e1"
_KEY_ID_SIZE = 4
_SALT_SIZE = 16
_NONCE_SIZE = 12
_TAG_SIZE = 16
_HEADER_SIZE = len(_MAGIC) + _KEY_ID_SIZE + _SALT_SIZE + _NONCE_SIZE
_HKDF_INFO = b"fastapi_alcazaba entry encryption v1"


def _key_id(secret: bytes) -> bytes:
    """Names the secret in each entry, to pick the right one when keys are rotated. A truncated
    hash: it reveals nothing about a secret of 32 bytes or more."""
    return hashlib.sha256(b"fastapi_alcazaba key id\0" + secret).digest()[:_KEY_ID_SIZE]


class Encrypter:
    """Encrypts what the cache stores and only accepts what it encrypted itself.

    On top of what signing gives (whoever can write to the backend can't plant data, run a pickle
    or move one user's entry under another user's key), a dump or a backup of Redis doesn't reveal
    the cached responses. The cache key is the associated data, so an entry only decrypts under
    its own key. Each entry is encrypted with its own AES-256 key, derived with HKDF-SHA256 from
    the secret and a random salt, so random 96-bit nonces never get near the limits of AES-GCM
    however many entries are written.

    The first secret encrypts; all of them decrypt, so a secret can be rotated without losing the
    cache: put the new one first and drop the old one later.

    Layout: b"fastapi_alcazaba/e1" | key id (4) | salt (16) | nonce (12) | ciphertext and GCM
    tag (16).
    """

    def __init__(self, keys: Secrets) -> None:
        if AESGCM is None:
            raise ImportError(
                "encrypted entries need the cryptography package: "
                "pip install 'fastapi-alcazaba[crypto]'"
            )
        secrets = secret_list(keys)
        self._current = _key_id(secrets[0])
        self._secrets = {_key_id(secret): secret for secret in secrets}

    @staticmethod
    def _cipher(secret: bytes, salt: bytes) -> AESGCM:
        hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_HKDF_INFO)
        return AESGCM(hkdf.derive(secret))

    def seal(self, cache_key: str, payload: bytes) -> bytes:
        salt, nonce = os.urandom(_SALT_SIZE), os.urandom(_NONCE_SIZE)
        cipher = self._cipher(self._secrets[self._current], salt)
        ciphertext = cipher.encrypt(nonce, payload, cache_key.encode())
        return _MAGIC + self._current + salt + nonce + ciphertext

    def open(self, cache_key: str, data: bytes) -> bytes | None:
        """The payload, or None when `data` wasn't encrypted by us for `cache_key`."""
        if len(data) < _HEADER_SIZE + _TAG_SIZE or not data.startswith(_MAGIC):
            return None
        position = len(_MAGIC)
        secret = self._secrets.get(data[position : position + _KEY_ID_SIZE])
        if secret is None:
            return None
        position += _KEY_ID_SIZE
        salt = data[position : position + _SALT_SIZE]
        position += _SALT_SIZE
        nonce = data[position : position + _NONCE_SIZE]
        cipher = self._cipher(secret, salt)
        try:
            return cipher.decrypt(nonce, data[_HEADER_SIZE:], cache_key.encode())
        except InvalidTag:
            return None
