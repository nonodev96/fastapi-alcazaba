"""The keys that sign and verify the audit trail: HMAC secrets (whoever verifies can also sign) or
Ed25519 key pairs (the public key verifies, only the private one signs: for an auditor who must
not be able to forge the trail).

Each signature names its algorithm and key (`hs256:<kid>:<hex>`, `ed25519:<kid>:<base64url>`), so
a trail written over several rotations verifies with all their keys at once.
"""

import base64
import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import SecretBytes, SecretStr

from fastapi_alcazaba.core.integrity import reveal, secret_list

#: What a key can be: an HMAC secret (32 bytes or more), an Ed25519 private key (it signs, and
#: verifies), an Ed25519 public key (it verifies), as cryptography's objects, PEM, DER or an
#: OpenSSH public key line; a list rotates them.
AuditKeys = Any

_KID_LABEL = b"fastapi-alcazaba audit kid"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover  # the tests have cryptography
        raise ImportError(
            'Ed25519 audit keys need cryptography: pip install "fastapi-alcazaba[crypto]"'
        ) from exc
    return ed25519


@dataclass(frozen=True, slots=True)
class AuditKey:
    """One key of the trail, parsed: its algorithm, its id, and what it can do."""

    algorithm: str  # "hs256" or "ed25519"
    kid: str
    _secret: bytes | None = None
    _private: Any = None
    _public: Any = None

    @property
    def can_sign(self) -> bool:
        return self._secret is not None or self._private is not None

    def sign(self, payload: bytes) -> str:
        """`payload`'s signature: `<algorithm>:<kid>:<value>`."""
        if self._secret is not None:
            value = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        elif self._private is not None:
            value = _b64(self._private.sign(payload))
        else:
            raise ValueError("a public key only verifies: sign with its private key")
        return f"{self.algorithm}:{self.kid}:{value}"

    def verifies(self, payload: bytes, value: str) -> bool:
        """Whether `value` (the part after the kid) is this key's signature of `payload`."""
        if self._secret is not None:
            expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest().encode()
            # As bytes: compare_digest refuses strings with non-ASCII characters.
            return hmac.compare_digest(value.encode("utf-8", "surrogateescape"), expected)
        from cryptography.exceptions import InvalidSignature

        try:
            signature = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            self._public.verify(signature, payload)
        except (InvalidSignature, ValueError):
            return False
        return True


def audit_keys(keys: AuditKeys) -> list[AuditKey]:
    """The keys of `keys` (one, or a list: the first signs), parsed and checked."""
    items = list(keys) if isinstance(keys, list | tuple) else [keys]
    if not items:
        raise ValueError("at least one key is needed")
    parsed = [_parse(item) for item in items]
    kids = [key.kid for key in parsed]
    if len(set(kids)) != len(kids):
        raise ValueError("the same key twice")
    return parsed


def _parse(key: Any) -> AuditKey:
    if isinstance(key, AuditKey):
        return key
    ed25519 = _maybe_ed25519()
    if ed25519 is not None and isinstance(key, ed25519.Ed25519PrivateKey):
        return _private(key)
    if ed25519 is not None and isinstance(key, ed25519.Ed25519PublicKey):
        return _public(key)
    value = reveal(key) if isinstance(key, str | bytes | SecretStr | SecretBytes) else key
    raw = value.encode() if isinstance(value, str) else value
    if not isinstance(raw, bytes):
        raise TypeError(f"not an audit key: {type(key).__name__}")
    # A public key must never be taken for an HMAC secret: whoever has it (anybody: it's public)
    # could then sign. Every form of one is read as a key, whatever comes before it.
    if (start := raw.find(b"-----BEGIN")) != -1:
        return _pem(raw[start:])
    if raw.lstrip().startswith((b"ssh-", b"ecdsa-")):
        return _openssh(raw.strip())
    if (der := _der(raw)) is not None:
        return der
    secret = secret_list(raw)[0]
    kid = _b64(hmac.new(secret, _KID_LABEL, hashlib.sha256).digest())[:11]
    return AuditKey("hs256", kid, _secret=secret)


def _maybe_ed25519() -> Any:
    try:
        return _ed25519()
    except ImportError:  # pragma: no cover  # the tests have cryptography
        return None


def _pem(raw: bytes) -> AuditKey:
    from cryptography.hazmat.primitives import serialization

    ed25519 = _ed25519()
    try:
        if b"PRIVATE KEY" in raw:
            private = serialization.load_pem_private_key(raw, password=None)
            if isinstance(private, ed25519.Ed25519PrivateKey):
                return _private(private)
        else:
            public = serialization.load_pem_public_key(raw)
            if isinstance(public, ed25519.Ed25519PublicKey):
                return _public(public)
    except ValueError as exc:
        raise ValueError(f"not a PEM key: {exc}") from None
    raise ValueError("an asymmetric audit key is an Ed25519 key")


def _openssh(raw: bytes) -> AuditKey:
    from cryptography.hazmat.primitives import serialization

    try:
        public = serialization.load_ssh_public_key(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"not an OpenSSH public key: {exc}") from None
    if not isinstance(public, _ed25519().Ed25519PublicKey):
        raise ValueError("an asymmetric audit key is an Ed25519 key")
    return _public(public)


def _der(raw: bytes) -> AuditKey | None:
    """The key in `raw` when it's one in DER (a SEQUENCE), None when it isn't one."""
    if not raw.startswith(b"\x30"):
        return None
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:  # pragma: no cover  # the tests have cryptography
        return None
    for load in (serialization.load_der_public_key, serialization.load_der_private_key):
        try:
            loaded = load(raw) if load is serialization.load_der_public_key else load(raw, None)
        except (ValueError, TypeError):
            continue
        ed25519 = _ed25519()
        if isinstance(loaded, ed25519.Ed25519PrivateKey):
            return _private(loaded)
        if isinstance(loaded, ed25519.Ed25519PublicKey):
            return _public(loaded)
        raise ValueError("an asymmetric audit key is an Ed25519 key")
    return None


def _kid(public: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(hashlib.sha256(raw).digest())[:11]


def _private(private: Any) -> AuditKey:
    public = private.public_key()
    return AuditKey("ed25519", _kid(public), _private=private, _public=public)


def _public(public: Any) -> AuditKey:
    return AuditKey("ed25519", _kid(public), _public=public)


def generate_audit_key_pair() -> tuple[str, str]:
    """A new Ed25519 key pair, as PEM: (the private key, for the app's `signing_key`; the public
    key, for the auditor's `verify_audit_trail` and `fastapi-alcazaba audit verify`)."""
    from cryptography.hazmat.primitives import serialization

    private = _ed25519().Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem.decode(), public_pem.decode()


def find_key(keys: Iterable[AuditKey], signature: str) -> tuple[AuditKey, str] | None:
    """The key that `signature` names, and its value; None when none of `keys` is it."""
    algorithm, _, rest = signature.partition(":")
    kid, _, value = rest.partition(":")
    for key in keys:
        if key.algorithm == algorithm and key.kid == kid:
            return key, value
    return None
