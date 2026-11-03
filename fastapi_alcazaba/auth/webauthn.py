"""Passkeys and security keys (WebAuthn): logins without a password, which a phishing page can't
relay, and a second factor that resists it. Needs the `crypto` extra (cryptography).

    webauthn = FastAPIWebAuthn(rp_id="example.com", rp_name="Example",
                               origins=["https://example.com"])

    options = await webauthn.registration_options(user.id, user.email)  # to the browser
    passkey = await webauthn.verify_registration(user.id, credential)   # its answer: store it

    options = await webauthn.authentication_options()                   # a login, any passkey
    passkey = await db.passkeys.get(credential_id(credential))
    passkey = await webauthn.verify_authentication(credential, passkey) # store it again

The browser takes the options with `PublicKeyCredential.parseCreationOptionsFromJSON()` (or
`parseRequestOptionsFromJSON()`), passes them to `navigator.credentials.create()` (or `get()`),
and sends back `credential.toJSON()`. Each challenge is random, used once, and expires.

The ceremonies are checked as WebAuthn Level 2 says (7.1 and 7.2): the challenge, the origin, the
relying party's ID hash, the flags (user present, user verified), the key's algorithm and the
signature, and the signature counter. The attestation isn't: `attestation: "none"` is asked, so
the app doesn't learn the authenticator's make, which passkeys don't need.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection

from fastapi_alcazaba.auth import stepup
from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
except ImportError:  # pragma: no cover  # the crypto extra isn't installed
    ec = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from fastapi_alcazaba.audit import FastAPIAudit

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: COSE algorithms (RFC 9053): ES256 (ECDSA P-256 and SHA-256), EdDSA (Ed25519), RS256.
ES256, EDDSA, RS256 = -7, -8, -257
ALGORITHMS = {ES256: "ES256", EDDSA: "EdDSA", RS256: "RS256"}
#: Whether the authenticator must check who uses it (a PIN, a fingerprint, the phone's lock).
UserVerification = Literal["required", "preferred", "discouraged"]

# The flags of authenticator data (WebAuthn 6.1).
_UP, _UV, _BE, _BS, _AT, _ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80
_MAX_CREDENTIAL_ID = 1023
_MAX_CBOR_DEPTH = 8
_MAX_JSON = 64 * 1024  # bytes of clientDataJSON, attestationObject...


class WebAuthnRefused(Exception):
    """A registration or a login that isn't valid: the user's answer is refused, a 400 or a 401
    for your route. `reason` says why, for the logs (not for the user).

    :param reason: short and stable: "challenge", "origin", "rp_id", "user_presence",
        "user_verification", "signature", "counter", "algorithm", "malformed"...
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Passkey:
    """A registered credential, to store with its user (as `to_dict()`, JSON) and to give back to
    `verify_authentication`. It holds a public key: nothing in it opens a door by itself.

    :param id: the credential's ID (base64url), by which the browser names it.
    :param public_key: its COSE public key (base64url).
    :param algorithm: its COSE algorithm: -7 (ES256), -8 (EdDSA) or -257 (RS256).
    :param user_handle: the user's handle (base64url) the authenticator keeps with it.
    :param sign_count: the signature counter it last showed (0 for those that don't count).
    :param transports: how the browser reaches it ("internal", "hybrid", "usb"...).
    :param backup_eligible: it can be synced (a passkey in iCloud Keychain, Google Password
        Manager).
    :param backed_up: it is synced now.
    :param created: when it was registered (Unix time).
    :param last_used: when it last logged someone in (Unix time); None until it does.
    """

    id: str
    public_key: str
    algorithm: int
    user_handle: str
    sign_count: int = 0
    transports: tuple[str, ...] = ()
    backup_eligible: bool = False
    backed_up: bool = False
    created: float = field(default_factory=time.time)
    last_used: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["transports"] = list(self.transports)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Passkey":
        return cls(**{**data, "transports": tuple(data.get("transports", ()))})


def credential_id(credential: Mapping[str, Any]) -> str:
    """The ID of the credential the browser answered with (base64url): what to look its Passkey
    up by, before `verify_authentication`."""
    found = credential.get("id")
    if not isinstance(found, str) or not found:
        raise WebAuthnRefused("malformed", "no credential id")
    return found


def _b64decode(value: Any, what: str) -> bytes:
    if not isinstance(value, str) or len(value) > _MAX_JSON * 4 // 3:
        raise WebAuthnRefused("malformed", f"{what} isn't base64url")
    try:
        # Strict: urlsafe_b64decode would drop what isn't base64url, and decode the rest.
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise WebAuthnRefused("malformed", f"{what} isn't base64url") from None


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class _CBOR:
    """The CBOR (RFC 8949) of attestation objects and COSE keys: integers, byte and text
    strings, arrays, maps, true, false and null; definite lengths only. Anything else (tags,
    floats, indefinite lengths, duplicate keys) is refused: an authenticator doesn't send it."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def item(self, depth: int = 0) -> Any:
        if depth > _MAX_CBOR_DEPTH:
            raise WebAuthnRefused("malformed", "CBOR nested too deep")
        initial = self.take(1)[0]
        major, info = initial >> 5, initial & 0x1F
        if major == 7:
            simple = {20: False, 21: True, 22: None}
            if info not in simple:
                raise WebAuthnRefused("malformed", "CBOR floats and simple values aren't read")
            return simple[info]
        length = self._length(info)
        if major == 0:
            return length
        if major == 1:
            return -1 - length
        if major == 2:
            return self.take(length)
        if major == 3:
            try:
                return self.take(length).decode()
            except UnicodeDecodeError:
                raise WebAuthnRefused("malformed", "CBOR text isn't UTF-8") from None
        if major == 4:
            self.room(length)
            return [self.item(depth + 1) for _ in range(length)]
        if major == 5:
            self.room(2 * length)
            found: dict[Any, Any] = {}
            for _ in range(length):
                key = self.item(depth + 1)
                if not isinstance(key, int | str) or isinstance(key, bool) or key in found:
                    raise WebAuthnRefused("malformed", "CBOR map keys are unique ints or texts")
                found[key] = self.item(depth + 1)
            return found
        raise WebAuthnRefused("malformed", "CBOR tags aren't read")

    def _length(self, info: int) -> int:
        if info < 24:
            return info
        sizes = {24: 1, 25: 2, 26: 4, 27: 8}
        if info not in sizes:
            raise WebAuthnRefused("malformed", "CBOR indefinite lengths aren't read")
        return int.from_bytes(self.take(sizes[info]), "big")

    def room(self, items: int) -> None:
        if items > len(self.data) - self.position:
            raise WebAuthnRefused("malformed", "CBOR shorter than it says")

    def take(self, size: int) -> bytes:
        self.room(size)
        taken = self.data[self.position : self.position + size]
        self.position += size
        return taken


def _cbor(data: bytes) -> Any:
    """The one CBOR item that `data` is, all of it."""
    reader = _CBOR(data)
    found = reader.item()
    if reader.position != len(data):
        raise WebAuthnRefused("malformed", "bytes after the CBOR item")
    return found


@dataclass(frozen=True, slots=True)
class _AuthenticatorData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    credential_id: bytes | None = None
    public_key: bytes | None = None  # the COSE key, as its CBOR


def _authenticator_data(data: bytes) -> _AuthenticatorData:
    """WebAuthn 6.1: the RP ID's hash, the flags, the counter, then the attested credential
    (with AT) and the extensions (with ED), and nothing else."""
    if len(data) < 37:
        raise WebAuthnRefused("malformed", "authenticator data too short")
    rp_id_hash, flags = data[:32], data[32]
    sign_count = struct.unpack(">I", data[33:37])[0]
    reader = _CBOR(data)
    reader.position = 37
    credential = public_key = None
    if flags & _AT:
        reader.room(18)
        start = reader.position + 16  # after the AAGUID
        length = struct.unpack(">H", data[start : start + 2])[0]
        if length > _MAX_CREDENTIAL_ID:
            raise WebAuthnRefused("malformed", "credential id too long")
        reader.position = start + 2
        credential = reader.take(length)
        key_start = reader.position
        reader.item()
        public_key = data[key_start : reader.position]
    if flags & _ED:
        if not isinstance(reader.item(), dict):
            raise WebAuthnRefused("malformed", "extensions aren't a map")
    if reader.position != len(data):
        raise WebAuthnRefused("malformed", "bytes after the authenticator data")
    if flags & _BS and not flags & _BE:
        raise WebAuthnRefused("malformed", "backed up but not backup eligible")
    return _AuthenticatorData(rp_id_hash, flags, sign_count, credential, public_key)


def _public_key(cose: bytes, algorithm: int) -> Any:
    """The `cryptography` public key of a COSE key (RFC 9053), checked against `algorithm`:
    an EC2 key on P-256 for ES256, an OKP Ed25519 key for EdDSA, RSA of 2048 bits or more for
    RS256."""
    key = _cbor(cose)
    if not isinstance(key, dict) or key.get(3) != algorithm:
        raise WebAuthnRefused("algorithm", "the key isn't for its algorithm")
    try:
        if algorithm == ES256 and key.get(1) == 2 and key.get(-1) == 1:
            x, y = key[-2], key[-3]
            if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
                raise ValueError
            numbers = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            )
            return numbers.public_key()  # ValueError for a point that isn't on the curve
        if algorithm == EDDSA and key.get(1) == 1 and key.get(-1) == 6:
            return ed25519.Ed25519PublicKey.from_public_bytes(key[-2])
        if algorithm == RS256 and key.get(1) == 3:
            n, e = int.from_bytes(key[-1], "big"), int.from_bytes(key[-2], "big")
            if n.bit_length() < 2048:
                raise ValueError
            return rsa.RSAPublicNumbers(e, n).public_key()
    except (KeyError, TypeError, ValueError):
        raise WebAuthnRefused("algorithm", "the public key isn't valid") from None
    raise WebAuthnRefused("algorithm", f"unsupported key for {ALGORITHMS.get(algorithm)}")


def _verify_signature(public_key: Any, algorithm: int, signature: bytes, data: bytes) -> None:
    try:
        if algorithm == ES256:
            public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif algorithm == EDDSA:
            public_key.verify(signature, data)
        else:
            public_key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        raise WebAuthnRefused("signature") from None


class FastAPIWebAuthn:
    """Passkeys and security keys: registering them (`registration_options`,
    `verify_registration`) and logging in with them (`authentication_options`,
    `verify_authentication`), as WebAuthn Level 2 says.

    - **The challenges** are random (32 bytes), kept in the cache's backend for `timeout`
      seconds, bound to the ceremony and to the user they were made for, and used once: of two
      answers to the same challenge, one is checked.
    - **The origin** of the answer must be one of `origins`, never in a cross-origin iframe, and
      the authenticator's data must be for `rp_id`: a phishing site's origin is refused, and so
      is its RP ID, which the browser wouldn't sign for another site.
    - **The user** must be present (a touch), and verified (a PIN, a fingerprint, the phone's
      lock) with `user_verification="required"`, the default: a passkey is then both factors.
    - **The counter:** a credential whose counter goes back (or doesn't move, for those that
      count) was cloned: refused. Synced passkeys count 0, and aren't checked.
    - **When the backend fails,** no answer is checked: the challenges can't be used once.

    :param rp_id: the relying party's ID: the site's domain, or one it's under
        (`example.com` for `https://login.example.com`). The credentials only work there.
    :param rp_name: the name the authenticator shows ("Example").
    :param origins: the origins the answers come from, `https://example.com` (or
        `http://localhost:8000`, in development); each on `rp_id` or under it.
    :param user_verification: required, preferred or discouraged.
    :param algorithms: the COSE algorithms accepted, in order of preference.
    :param timeout: seconds a challenge lasts, and the browser waits for the user.
    :param cache: the Cache whose backend keeps the challenges; FastAPICache's by default.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    :param audit: the audit trail that records each passkey registered
        (`auth.passkey.register`) and each login with one (`auth.passkey`), with their outcome.
    """

    def __init__(
        self,
        *,
        rp_id: str,
        rp_name: str,
        origins: Collection[str],
        user_verification: UserVerification = "required",
        algorithms: Sequence[int] = (ES256, EDDSA, RS256),
        timeout: int = 300,
        cache: Cache | None = None,
        namespace: str = "webauthn",
        audit: "FastAPIAudit | None" = None,
    ) -> None:
        if ec is None:
            raise ImportError(
                "WebAuthn needs the cryptography package: pip install 'fastapi-alcazaba[crypto]'"
            )
        if not rp_id or rp_id != rp_id.lower() or any(c in rp_id for c in "/:@ "):
            raise ValueError("rp_id is a domain in lower case, without a scheme nor a port")
        if isinstance(origins, str) or not origins:
            raise ValueError("origins is a list of the site's origins")
        for origin in origins:
            parts = urlsplit(origin)
            host = parts.hostname or ""
            local = parts.scheme == "http" and host == "localhost"
            if not (parts.scheme == "https" or local) or parts.path or parts.query:
                raise ValueError(f"an origin is https://host[:port] or http://localhost: {origin}")
            if host != rp_id and not host.endswith(f".{rp_id}"):
                raise ValueError(f"the origin {origin} isn't on the RP ID {rp_id}")
        if user_verification not in ("required", "preferred", "discouraged"):
            raise ValueError("user_verification is required, preferred or discouraged")
        if not algorithms or any(algorithm not in ALGORITHMS for algorithm in algorithms):
            raise ValueError("the algorithms are -7 (ES256), -8 (EdDSA) and -257 (RS256)")
        if not 30 <= timeout <= 600:
            raise ValueError("timeout is 30 to 600 seconds")
        self.rp_id = rp_id
        self.rp_name = rp_name
        self.origins = frozenset(origins)
        self.user_verification = user_verification
        self.algorithms = list(algorithms)
        self.timeout = timeout
        self.cache = cache
        self.namespace = namespace
        self.audit = audit
        self._rp_id_hash = hashlib.sha256(rp_id.encode()).digest()

    # Registration.

    async def registration_options(
        self,
        user_id: stepup.UserId,
        user_name: str,
        *,
        display_name: str | None = None,
        user_handle: str | None = None,
        exclude: Sequence[Passkey] = (),
        resident_key: Literal["required", "preferred", "discouraged"] = "required",
    ) -> dict[str, Any]:
        """The options of `navigator.credentials.create()`, as JSON, for `user_id` to register
        a passkey. `user_name` and `display_name` are what the authenticator shows;
        `user_handle` is the user's handle (base64url) when they have passkeys already, else a
        new random one. `exclude` are their passkeys, which the authenticator won't register
        twice. `resident_key="required"` makes a passkey, which logs in without a username."""
        handle = user_handle if user_handle is not None else _b64encode(secrets.token_bytes(32))
        if not 1 <= len(_b64decode(handle, "the user handle")) <= 64:
            raise ValueError("a user handle is 1 to 64 bytes")
        challenge = await self._challenge("create", user_id, handle)
        return {
            "rp": {"id": self.rp_id, "name": self.rp_name},
            "user": {"id": handle, "name": user_name, "displayName": display_name or user_name},
            "challenge": challenge,
            "pubKeyCredParams": [
                {"type": "public-key", "alg": algorithm} for algorithm in self.algorithms
            ],
            "timeout": self.timeout * 1000,
            "excludeCredentials": [
                {"type": "public-key", "id": passkey.id, "transports": list(passkey.transports)}
                for passkey in exclude
            ],
            "authenticatorSelection": {
                "residentKey": resident_key,
                "requireResidentKey": resident_key == "required",
                "userVerification": self.user_verification,
            },
            "attestation": "none",
        }

    async def verify_registration(
        self,
        user_id: stepup.UserId,
        credential: Mapping[str, Any],
        *,
        request: HTTPConnection | None = None,
    ) -> Passkey:
        """The Passkey of `credential` (the browser's `credential.toJSON()` of `create()`), to
        store with `user_id`; WebAuthnRefused when it isn't a valid answer to a challenge made
        for them. Check that no other user has its `id` before storing it."""
        try:
            passkey = await self._registration(user_id, credential)
        except WebAuthnRefused as refused:
            self._count("register", refused.reason)
            await self._audit("auth.passkey.register", refused.reason, user_id, request)
            raise
        self._count("register", "success")
        await self._audit("auth.passkey.register", "success", user_id, request)
        return passkey

    async def _registration(self, user_id: stepup.UserId, credential: Mapping[str, Any]) -> Passkey:
        response = _response(credential)
        client_data = _b64decode(response.get("clientDataJSON"), "clientDataJSON")
        challenge = self._client_data(client_data, "webauthn.create")
        pending = await self._use_challenge(challenge, "create")
        if pending["user"] != str(user_id):
            raise WebAuthnRefused("challenge", "made for another user")
        attestation = _cbor(_b64decode(response.get("attestationObject"), "attestationObject"))
        if not isinstance(attestation, dict) or not isinstance(attestation.get("authData"), bytes):
            raise WebAuthnRefused("malformed", "the attestation object has no authData")
        data = _authenticator_data(attestation["authData"])
        self._check_flags(data)
        if data.credential_id is None or data.public_key is None:
            raise WebAuthnRefused("malformed", "no attested credential")
        if _b64decode(credential.get("rawId", credential.get("id")), "rawId") != data.credential_id:
            raise WebAuthnRefused("malformed", "the credential id isn't the attested one")
        key = _cbor(data.public_key)
        algorithm = key.get(3) if isinstance(key, dict) else None
        if algorithm not in self.algorithms:
            raise WebAuthnRefused("algorithm", f"{algorithm!r} isn't accepted")
        _public_key(data.public_key, algorithm)  # refused here, not at the first login
        transports = response.get("transports", [])
        if not isinstance(transports, list) or not all(isinstance(t, str) for t in transports):
            transports = []
        return Passkey(
            id=_b64encode(data.credential_id),
            public_key=_b64encode(data.public_key),
            algorithm=algorithm,
            user_handle=pending["handle"],
            sign_count=data.sign_count,
            transports=tuple(transports[:10]),
            backup_eligible=bool(data.flags & _BE),
            backed_up=bool(data.flags & _BS),
        )

    # Logging in.

    async def authentication_options(
        self,
        user_id: stepup.UserId | None = None,
        *,
        allow: Sequence[Passkey] = (),
    ) -> dict[str, Any]:
        """The options of `navigator.credentials.get()`, as JSON. Without a user, any passkey of
        this site may answer (a login without a username, or the browser's autofill); with
        `user_id` and `allow` (their passkeys), only theirs, for a second factor."""
        challenge = await self._challenge("get", user_id, None)
        return {
            "challenge": challenge,
            "rpId": self.rp_id,
            "timeout": self.timeout * 1000,
            "allowCredentials": [
                {"type": "public-key", "id": passkey.id, "transports": list(passkey.transports)}
                for passkey in allow
            ],
            "userVerification": self.user_verification,
        }

    async def verify_authentication(
        self,
        credential: Mapping[str, Any],
        passkey: Passkey,
        *,
        user_id: stepup.UserId | None = None,
        request: HTTPConnection | None = None,
    ) -> Passkey:
        """The Passkey with its new counter and `last_used`, to store again, when `credential`
        (the browser's `credential.toJSON()` of `get()`) is a valid answer by `passkey`, the one
        whose `id` is `credential_id(credential)`; WebAuthnRefused otherwise. `user_id` is the
        user `passkey` is stored with: the challenge, when made for a user, must be theirs."""
        try:
            updated = await self._authentication(credential, passkey, user_id)
        except WebAuthnRefused as refused:
            self._count("authenticate", refused.reason)
            await self._audit("auth.passkey", refused.reason, user_id, request)
            raise
        self._count("authenticate", "success")
        await self._audit("auth.passkey", "success", user_id, request)
        return updated

    async def _authentication(
        self,
        credential: Mapping[str, Any],
        passkey: Passkey,
        user_id: stepup.UserId | None,
    ) -> Passkey:
        if not hmac.compare_digest(credential_id(credential), passkey.id):
            raise WebAuthnRefused("malformed", "the credential isn't this passkey")
        raw_id = _b64decode(credential.get("rawId", credential["id"]), "rawId")
        if raw_id != _b64decode(passkey.id, "id"):
            raise WebAuthnRefused("malformed", "rawId isn't the id")
        response = _response(credential)
        client_data = _b64decode(response.get("clientDataJSON"), "clientDataJSON")
        challenge = self._client_data(client_data, "webauthn.get")
        pending = await self._use_challenge(challenge, "get")
        if pending["user"] is not None and pending["user"] != str(user_id):
            raise WebAuthnRefused("challenge", "made for another user")
        handle = response.get("userHandle")
        if handle not in (None, "") and not hmac.compare_digest(
            _b64decode(handle, "userHandle"), _b64decode(passkey.user_handle, "user_handle")
        ):
            raise WebAuthnRefused("user_handle", "the authenticator's user isn't the passkey's")
        if pending["user"] is None and handle in (None, ""):
            # A login without a username: only the user handle says whose passkey answered.
            raise WebAuthnRefused("user_handle", "no user handle")
        raw = _b64decode(response.get("authenticatorData"), "authenticatorData")
        data = _authenticator_data(raw)
        self._check_flags(data)
        if bool(data.flags & _BE) != passkey.backup_eligible:
            raise WebAuthnRefused("backup", "backup eligibility changed")
        signature = _b64decode(response.get("signature"), "signature")
        public_key = _public_key(_b64decode(passkey.public_key, "public_key"), passkey.algorithm)
        _verify_signature(
            public_key, passkey.algorithm, signature, raw + hashlib.sha256(client_data).digest()
        )
        if (data.sign_count or passkey.sign_count) and data.sign_count <= passkey.sign_count:
            raise WebAuthnRefused("counter", "the counter went back: a cloned authenticator")
        return replace(
            passkey,
            sign_count=data.sign_count,
            backed_up=bool(data.flags & _BS),
            last_used=time.time(),
        )

    # Step-up: a login with a verified passkey is a second factor.

    def mark(self, request: HTTPConnection, user_id: stepup.UserId) -> None:
        """Note in the session that `user_id` just logged in with a passkey (`stepup.mark`),
        for the routes of `require_recent`, and give the session a new id."""
        stepup.mark(request, user_id, "passkey")

    def require_recent(
        self,
        user_id: Callable[..., Any],
        max_age: int = 300,
        **options: Any,
    ) -> Callable[..., Any]:
        """A dependency that needs a second factor (a passkey, or a TOTP code) of the last
        `max_age` seconds in this session: `stepup.require_recent`, with its `methods` (e.g.
        `{"passkey"}` for passkeys only), `claims`, `acr_values` and `audit`."""
        return stepup.require_recent(user_id, max_age, **options)

    # What they share.

    def _client_data(self, client_data: bytes, ceremony: str) -> bytes:
        """WebAuthn 7.1 and 7.2, steps on the client data: its type, origin, and challenge."""
        if len(client_data) > _MAX_JSON:
            raise WebAuthnRefused("malformed", "clientDataJSON too long")
        try:
            data = json.loads(client_data)
        except ValueError:
            raise WebAuthnRefused("malformed", "clientDataJSON isn't JSON") from None
        if not isinstance(data, dict) or data.get("type") != ceremony:
            raise WebAuthnRefused("malformed", f"the client data isn't a {ceremony}")
        if data.get("origin") not in self.origins:
            raise WebAuthnRefused("origin", f"{str(data.get('origin'))[:100]!r} isn't the site's")
        if data.get("crossOrigin") is True:
            raise WebAuthnRefused("origin", "in a cross-origin iframe")
        return _b64decode(data.get("challenge"), "the challenge")

    def _check_flags(self, data: _AuthenticatorData) -> None:
        if not hmac.compare_digest(data.rp_id_hash, self._rp_id_hash):
            raise WebAuthnRefused("rp_id", "the authenticator data is for another site")
        if not data.flags & _UP:
            raise WebAuthnRefused("user_presence", "the user didn't touch the authenticator")
        if self.user_verification == "required" and not data.flags & _UV:
            raise WebAuthnRefused("user_verification", "the user wasn't verified")

    async def _challenge(
        self,
        ceremony: str,
        user_id: stepup.UserId | None,
        handle: str | None,
    ) -> str:
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, str | int | uuid.UUID)
        ):
            raise TypeError("user_id is an ID: a str, an int or a UUID")
        challenge = secrets.token_bytes(32)
        cache = resolve_cache(self.cache)
        backend = shared_backend(cache.backend)
        key = self._key(cache, challenge)
        pending = {"ceremony": ceremony, "user": None if user_id is None else str(user_id)}
        if handle is not None:
            pending["handle"] = handle
        value = json.dumps(pending).encode()
        await cache.guard.run("set", lambda: backend.set(key, value, self.timeout))
        return _b64encode(challenge)

    async def _use_challenge(self, challenge: bytes, ceremony: str) -> dict[str, Any]:
        """The challenge's record, used up: of two answers to it, the one that deletes it."""
        cache = resolve_cache(self.cache)
        backend = shared_backend(cache.backend)
        key = self._key(cache, challenge)
        try:
            stored = await cache.guard.run("get", lambda: backend.get(key))
            if stored is None:
                raise WebAuthnRefused("challenge", "unknown, expired or used")
            if await cache.guard.run("delete", lambda: backend.clear(key=key)) != 1:
                raise WebAuthnRefused("challenge", "used by another answer meanwhile")
        except BackendUnavailable as exc:
            logger.warning("Couldn't check a WebAuthn challenge", exc_info=exc)
            raise WebAuthnRefused("unchecked", "the backend failed") from exc
        pending: dict[str, Any] = json.loads(stored)
        if pending.get("ceremony") != ceremony:
            raise WebAuthnRefused("challenge", f"made for a {pending.get('ceremony')}")
        return pending

    def _key(self, cache: Cache, challenge: bytes) -> str:
        return cache.state_key(self.namespace, hashlib.sha256(challenge).hexdigest())

    def _count(self, ceremony: str, outcome: str) -> None:
        resolve_cache(self.cache).metrics.count("webauthn", ceremony=ceremony, outcome=outcome)

    async def _audit(
        self,
        action: str,
        outcome: str,
        user_id: stepup.UserId | None,
        request: HTTPConnection | None,
    ) -> None:
        if self.audit is None:
            return
        success = outcome == "success"
        await self.audit.log(
            action,
            outcome="success" if success else "failure",
            actor=user_id,
            reason=None if success else outcome,
            request=request,
        )


def _response(credential: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(credential, Mapping) or credential.get("type") != "public-key":
        raise WebAuthnRefused("malformed", "not a public-key credential")
    response = credential.get("response")
    if not isinstance(response, Mapping):
        raise WebAuthnRefused("malformed", "no response")
    return response
