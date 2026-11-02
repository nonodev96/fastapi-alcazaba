"""FastAPIWebAuthn: the ceremonies of WebAuthn Level 2, checked against real browsers' answers
(webauthn_vectors.json) and a software authenticator that breaks each rule in turn."""

# pylint: disable=protected-access  # the vectors' challenges are put where the app's would be

import base64
import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from fastapi_alcazaba import Cache, FastAPIAudit
from fastapi_alcazaba.audit import MemoryAuditSink
from fastapi_alcazaba.auth import FastAPIWebAuthn, Passkey, WebAuthnRefused, credential_id
from fastapi_alcazaba.auth import webauthn as webauthn_module
from fastapi_alcazaba.backends.inmemory import InMemoryBackend

ORIGIN = "https://example.com"
VECTORS: dict[str, Any] = json.loads((Path(__file__).parent / "webauthn_vectors.json").read_text())


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def cbor(value: Any) -> bytes:
    """A CBOR encoder, for the software authenticator."""

    def head(major: int, length: int) -> bytes:
        if length < 24:
            return bytes([major << 5 | length])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if length < 256**size:
                return bytes([major << 5 | info]) + length.to_bytes(size, "big")
        raise ValueError(length)

    if value is True or value is False or value is None:
        return bytes([0xF5 if value is True else 0xF4 if value is False else 0xF6])
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        encoded = value.encode()
        return head(3, len(encoded)) + encoded
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(item) for item in value)
    return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())


class Authenticator:
    """A software authenticator: makes credentials and signs, as a browser's would."""

    def __init__(self, algorithm: int = -7, rp_id: str = "example.com") -> None:
        self.algorithm = algorithm
        self.rp_id = rp_id
        if algorithm == -7:
            self.key: Any = ec.generate_private_key(ec.SECP256R1())
        elif algorithm == -8:
            self.key = ed25519.Ed25519PrivateKey.generate()
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.credential_id = b"\x01" * 16 + algorithm.to_bytes(2, "big", signed=True)
        self.count = 0
        self.flags = 0x01 | 0x04  # user present and verified
        self.origin = ORIGIN
        self.cross_origin = False
        self.ceremony: str | None = None  # another type of client data

    def cose(self) -> dict[int, Any]:
        public = self.key.public_key()
        if self.algorithm == -7:
            numbers = public.public_numbers()
            return {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        if self.algorithm == -8:
            from cryptography.hazmat.primitives import serialization

            raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            return {1: 1, 3: -8, -1: 6, -2: raw}
        numbers = public.public_numbers()
        return {1: 3, 3: -257, -1: numbers.n.to_bytes(256, "big"), -2: numbers.e.to_bytes(3, "big")}

    def data(self, attested: bool, flags: int | None = None, extra: bytes = b"") -> bytes:
        flags = (self.flags if flags is None else flags) | (0x40 if attested else 0)
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags])
        data += struct.pack(">I", self.count)
        if attested:
            data += b"\0" * 16 + struct.pack(">H", len(self.credential_id)) + self.credential_id
            data += cbor(self.cose())
        return data + extra

    def client_data(self, ceremony: str, challenge: str) -> bytes:
        return json.dumps(
            {
                "type": self.ceremony or ceremony,
                "challenge": challenge,
                "origin": self.origin,
                "crossOrigin": self.cross_origin,
            }
        ).encode()

    def create(self, options: dict[str, Any], **changes: Any) -> dict[str, Any]:
        """`credential.toJSON()` of `navigator.credentials.create(options)`."""
        attestation = {"fmt": "none", "attStmt": {}, "authData": self.data(True)}
        attestation.update(changes.pop("attestation", {}))
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(self.client_data("webauthn.create", options["challenge"])),
                "attestationObject": b64(cbor(attestation)),
                "transports": ["internal", "hybrid"],
            },
            **changes,
        }

    def get(
        self,
        options: dict[str, Any],
        user_handle: str | None,
        **changes: Any,
    ) -> dict[str, Any]:
        """`credential.toJSON()` of `navigator.credentials.get(options)`."""
        self.count += 1
        data = changes.pop("data", None) or self.data(False)
        client_data = self.client_data("webauthn.get", options["challenge"])
        signed = data + hashlib.sha256(client_data).digest()
        if self.algorithm == -7:
            signature = self.key.sign(signed, ec.ECDSA(hashes.SHA256()))
        elif self.algorithm == -8:
            signature = self.key.sign(signed)
        else:
            signature = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        response = {
            "clientDataJSON": b64(client_data),
            "authenticatorData": b64(data),
            "signature": b64(changes.pop("signature", signature)),
            "userHandle": user_handle,
        }
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": response,
            **changes,
        }


def make(**options: Any) -> FastAPIWebAuthn:
    return FastAPIWebAuthn(
        **{
            "rp_id": "example.com",
            "rp_name": "Example",
            "origins": [ORIGIN],
            "cache": Cache(InMemoryBackend()),
            **options,
        }
    )


async def registered(
    webauthn: FastAPIWebAuthn,
    authenticator: Authenticator,
    user_id: str = "u1",
) -> Passkey:
    options = await webauthn.registration_options(user_id, "ana@example.com")
    return await webauthn.verify_registration(user_id, authenticator.create(options))


async def refused(reason: str, coroutine: Any) -> None:
    with pytest.raises(WebAuthnRefused) as caught:
        await coroutine
    assert caught.value.reason == reason, caught.value


class TestRealBrowsers:
    """Answers of real browsers and authenticators, for their challenge, on
    http://localhost:5000: what was registered and signed with real keys, not with the software
    authenticator below."""

    def webauthn(self, **options: Any) -> FastAPIWebAuthn:
        return make(rp_id="localhost", origins=["http://localhost:5000"], **options)

    async def pending(
        self,
        webauthn: FastAPIWebAuthn,
        challenge: str,
        ceremony: str,
        user: str | None = "u1",
        handle: str = "aGFuZGxl",
    ) -> None:
        """The challenge as `registration_options` or `authentication_options` would keep it."""
        cache = webauthn.cache
        assert cache is not None
        record = json.dumps({"ceremony": ceremony, "user": user, "handle": handle}).encode()
        await cache.backend.set(webauthn._key(cache, unb64(challenge)), record, 60)

    async def test_registration_with_none_attestation(self) -> None:
        webauthn = self.webauthn()
        vector = VECTORS["registration_none"]
        await self.pending(webauthn, vector["challenge"], "create")
        credential = vector["credential"]
        passkey = await webauthn.verify_registration("u1", credential)
        assert passkey.id == credential["id"]
        assert (passkey.algorithm, passkey.sign_count) == (-7, 23)
        assert (passkey.transports, passkey.backup_eligible) == (("nfc", "usb"), False)
        assert passkey.user_handle == "aGFuZGxl"
        assert unb64(passkey.public_key)[:1] == b"\xa5"  # its COSE key: a map of five

    def test_a_packed_attestation_is_read(self) -> None:
        """A YubiKey's: its attestation isn't checked (attestation "none" is asked), its
        credential is read."""
        attestation = webauthn_module._cbor(unb64(VECTORS["attestation_packed"]))
        assert attestation["fmt"] == "packed"
        data = webauthn_module._authenticator_data(attestation["authData"])
        assert data.credential_id is not None
        assert (len(data.credential_id), data.sign_count) == (64, 42)

    @pytest.mark.parametrize("vector", VECTORS["logins"], ids=["EC2", "RSA", "OKP"])
    async def test_logins(self, vector: dict[str, Any]) -> None:
        webauthn = self.webauthn(user_verification="preferred")
        await self.pending(webauthn, vector["challenge"], "get")
        handle = vector["response"].get("userHandle", "aGFuZGxl")
        passkey = Passkey(
            vector["id"],
            vector["public_key"],
            vector["algorithm"],
            handle,
            sign_count=vector["sign_count"],
        )
        credential = {
            "id": vector["id"],
            "rawId": vector["id"],
            "type": "public-key",
            "response": vector["response"],
        }
        updated = await webauthn.verify_authentication(credential, passkey, user_id="u1")
        assert updated.sign_count == vector["new_sign_count"]
        assert updated.last_used is not None
        # The same answer again: its challenge is used up.
        again = webauthn.verify_authentication(credential, updated, user_id="u1")
        await refused("challenge", again)

    async def test_a_real_login_with_another_key_fails(self) -> None:
        webauthn = self.webauthn(user_verification="preferred")
        vector = VECTORS["logins"][0]
        await self.pending(webauthn, vector["challenge"], "get")
        other = b64(cbor(Authenticator().cose()))
        passkey = Passkey(vector["id"], other, -7, "aGFuZGxl", sign_count=vector["sign_count"])
        credential = {
            "id": vector["id"],
            "rawId": vector["id"],
            "type": "public-key",
            "response": vector["response"],
        }
        await refused(
            "signature", webauthn.verify_authentication(credential, passkey, user_id="u1")
        )


class TestRegistration:
    @pytest.mark.parametrize("algorithm", [-7, -8, -257])
    async def test_each_algorithm(self, algorithm: int) -> None:
        webauthn = make()
        authenticator = Authenticator(algorithm)
        passkey = await registered(webauthn, authenticator)
        assert passkey.algorithm == algorithm
        assert passkey.transports == ("internal", "hybrid")
        assert Passkey.from_dict(json.loads(json.dumps(passkey.to_dict()))) == passkey

    async def test_the_options(self) -> None:
        webauthn = make(user_verification="preferred")
        existing = Passkey("Y3JlZA", "a2V5", -7, "aGFuZGxl", transports=("usb",))
        options = await webauthn.registration_options(
            "u1", "ana@example.com", display_name="Ana", user_handle="aGFuZGxl", exclude=[existing]
        )
        assert options["rp"] == {"id": "example.com", "name": "Example"}
        assert options["user"] == {
            "id": "aGFuZGxl",
            "name": "ana@example.com",
            "displayName": "Ana",
        }
        assert len(unb64(options["challenge"])) == 32
        assert [p["alg"] for p in options["pubKeyCredParams"]] == [-7, -8, -257]
        assert options["excludeCredentials"] == [
            {"type": "public-key", "id": "Y3JlZA", "transports": ["usb"]}
        ]
        assert options["authenticatorSelection"] == {
            "residentKey": "required",
            "requireResidentKey": True,
            "userVerification": "preferred",
        }
        assert (options["attestation"], options["timeout"]) == ("none", 300_000)
        new = await webauthn.registration_options("u2", "ben")
        assert len(unb64(new["user"]["id"])) == 32  # a new random handle

    async def test_another_users_challenge(self) -> None:
        webauthn = make()
        options = await webauthn.registration_options("u1", "ana")
        await refused(
            "challenge", webauthn.verify_registration("u2", Authenticator().create(options))
        )

    async def test_each_challenge_once(self) -> None:
        webauthn = make()
        options = await webauthn.registration_options("u1", "ana")
        answer = Authenticator().create(options)
        await webauthn.verify_registration("u1", answer)
        await refused("challenge", webauthn.verify_registration("u1", answer))

    async def test_an_authentication_challenge_doesnt_register(self) -> None:
        webauthn = make()
        options = await webauthn.authentication_options("u1")
        authenticator = Authenticator()
        authenticator.ceremony = "webauthn.create"
        await refused(
            "challenge", webauthn.verify_registration("u1", authenticator.create(options))
        )

    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            ({"origin": "https://evil.example"}, "origin"),
            ({"origin": "https://login.example.com"}, "origin"),  # not one of the origins
            ({"cross_origin": True}, "origin"),
            ({"rp_id": "evil.example"}, "rp_id"),
            ({"flags": 0x04}, "user_presence"),
            ({"flags": 0x01}, "user_verification"),
            ({"flags": 0x01 | 0x04 | 0x10}, "malformed"),  # backed up, not eligible
            ({"ceremony": "webauthn.get"}, "malformed"),
        ],
    )
    async def test_what_the_authenticator_says(self, change: dict[str, Any], reason: str) -> None:
        webauthn = make()
        authenticator = Authenticator()
        for name, value in change.items():
            setattr(authenticator, name, value)
        options = await webauthn.registration_options("u1", "ana")
        await refused(reason, webauthn.verify_registration("u1", authenticator.create(options)))

    async def test_algorithms_not_accepted(self) -> None:
        webauthn = make(algorithms=[-7])
        options = await webauthn.registration_options("u1", "ana")
        await refused(
            "algorithm", webauthn.verify_registration("u1", Authenticator(-8).create(options))
        )

    @pytest.mark.parametrize(
        "key",
        [
            {1: 2, 3: -7, -1: 1, -2: b"\x01" * 32, -3: b"\x02" * 32},  # not on the curve
            {1: 2, 3: -7, -1: 2, -2: b"\x01" * 32, -3: b"\x02" * 32},  # P-384
            {
                1: 3,
                3: -257,
                -1: (2**1023 + 1).to_bytes(128, "big"),
                -2: b"\x01\x00\x01",
            },  # 1024 bits
            {1: 1, 3: -7, -1: 6, -2: b"\x00" * 32},  # an OKP key for ES256
        ],
    )
    async def test_keys_that_arent_valid(self, key: dict[int, Any]) -> None:
        webauthn = make()
        authenticator = Authenticator()
        authenticator.cose = lambda: key  # type: ignore[method-assign]
        options = await webauthn.registration_options("u1", "ana")
        await refused(
            "algorithm", webauthn.verify_registration("u1", authenticator.create(options))
        )

    async def test_the_credential_id_must_be_the_attested_one(self) -> None:
        webauthn = make()
        options = await webauthn.registration_options("u1", "ana")
        answer = Authenticator().create(options, rawId=b64(b"another"))
        await refused("malformed", webauthn.verify_registration("u1", answer))

    async def test_no_attested_credential(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        options = await webauthn.registration_options("u1", "ana")
        answer = authenticator.create(options, attestation={"authData": authenticator.data(False)})
        await refused("malformed", webauthn.verify_registration("u1", answer))


class TestAuthentication:
    async def test_a_login_with_a_passkey(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options()
        assert options["allowCredentials"] == []
        assert options["rpId"] == "example.com"
        answer = authenticator.get(options, passkey.user_handle)
        assert credential_id(answer) == passkey.id
        updated = await webauthn.verify_authentication(answer, passkey)
        assert updated.sign_count == 1

    async def test_a_second_factor_for_a_known_user(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options("u1", allow=[passkey])
        assert options["allowCredentials"][0]["id"] == passkey.id
        answer = authenticator.get(options, None)  # security keys may give no user handle
        await webauthn.verify_authentication(answer, passkey, user_id="u1")
        options = await webauthn.authentication_options("u1", allow=[passkey])
        await refused(
            "challenge",
            webauthn.verify_authentication(authenticator.get(options, None), passkey, user_id="u2"),
        )

    async def test_without_a_user_the_user_handle_is_needed(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options()
        await refused(
            "user_handle", webauthn.verify_authentication(authenticator.get(options, None), passkey)
        )
        options = await webauthn.authentication_options()
        other = b64(b"someone else")
        await refused(
            "user_handle",
            webauthn.verify_authentication(authenticator.get(options, other), passkey),
        )

    async def test_a_cloned_authenticator(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options()
        updated = await webauthn.verify_authentication(
            authenticator.get(options, passkey.user_handle), passkey
        )
        authenticator.count = 0  # the clone's counter lags behind
        options = await webauthn.authentication_options()
        await refused(
            "counter",
            webauthn.verify_authentication(
                authenticator.get(options, passkey.user_handle), updated
            ),
        )

    async def test_synced_passkeys_count_zero(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        authenticator.flags |= 0x08 | 0x10  # backup eligible, backed up
        passkey = await registered(webauthn, authenticator)
        assert (passkey.backup_eligible, passkey.backed_up) == (True, True)
        for _ in range(2):
            authenticator.count = -1  # get() adds one: always 0
            options = await webauthn.authentication_options()
            passkey = await webauthn.verify_authentication(
                authenticator.get(options, passkey.user_handle), passkey
            )
        assert passkey.sign_count == 0

    async def test_backup_eligibility_doesnt_change(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        authenticator.flags |= 0x08
        options = await webauthn.authentication_options()
        await refused(
            "backup",
            webauthn.verify_authentication(
                authenticator.get(options, passkey.user_handle), passkey
            ),
        )

    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            ({"origin": "https://evil.example"}, "origin"),
            ({"cross_origin": True}, "origin"),
            ({"rp_id": "evil.example"}, "rp_id"),
            ({"flags": 0x04}, "user_presence"),
            ({"flags": 0x01}, "user_verification"),
            ({"ceremony": "webauthn.create"}, "malformed"),
        ],
    )
    async def test_what_the_authenticator_says(self, change: dict[str, Any], reason: str) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        for name, value in change.items():
            setattr(authenticator, name, value)
        options = await webauthn.authentication_options()
        await refused(
            reason,
            webauthn.verify_authentication(
                authenticator.get(options, passkey.user_handle), passkey
            ),
        )

    async def test_a_signature_of_something_else(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options()
        answer = authenticator.get(options, passkey.user_handle, signature=b"\x30\x00")
        await refused("signature", webauthn.verify_authentication(answer, passkey))

    async def test_another_passkey(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options()
        other = Passkey("b3RoZXI", passkey.public_key, -7, passkey.user_handle)
        await refused(
            "malformed",
            webauthn.verify_authentication(authenticator.get(options, passkey.user_handle), other),
        )

    async def test_a_registration_challenge_doesnt_log_in(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.registration_options("u1", "ana")
        authenticator.ceremony = "webauthn.get"
        answer = authenticator.get(options, passkey.user_handle)
        await refused("challenge", webauthn.verify_authentication(answer, passkey, user_id="u1"))

    async def test_a_failing_backend_checks_nothing(self) -> None:
        class Down(InMemoryBackend):
            async def get(self, key: str) -> bytes | None:
                raise ConnectionError("down")

        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options()
        webauthn.cache = Cache(Down())
        await refused(
            "unchecked",
            webauthn.verify_authentication(
                authenticator.get(options, passkey.user_handle), passkey
            ),
        )


class TestMalformedAnswers:
    """What a browser never sends, refused as malformed before anything else is checked."""

    async def answer(self, **changes: Any) -> tuple[FastAPIWebAuthn, dict[str, Any]]:
        webauthn = make()
        options = await webauthn.registration_options("u1", "ana")
        answer = Authenticator().create(options)
        for path, value in changes.items():
            target = answer
            *parents, last = path.split("__")
            for parent in parents:
                target = target[parent]
            target[last] = value
        return webauthn, answer

    @pytest.mark.parametrize(
        "changes",
        [
            {"type": "password"},
            {"response": "nope"},
            {"response__clientDataJSON": 42},
            {"response__clientDataJSON": "abc$def"},  # not base64url
            {"response__clientDataJSON": b64(b"not json")},
            {"response__clientDataJSON": b64(b"[1, 2]")},
            {"response__clientDataJSON": b64(b"x" * 70_000)},
            {"response__attestationObject": b64(cbor({"fmt": "none"}))},  # no authData
        ],
    )
    async def test_refused(self, changes: dict[str, Any]) -> None:
        webauthn, answer = await self.answer(**changes)
        await refused("malformed", webauthn.verify_registration("u1", answer))

    async def test_transports_that_arent_strings_are_ignored(self) -> None:
        webauthn, answer = await self.answer(response__transports=[1, {"a": 2}])
        assert (await webauthn.verify_registration("u1", answer)).transports == ()

    async def test_a_cose_key_for_another_algorithm(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        key = authenticator.cose()
        authenticator.cose = lambda: {**key, 3: -257}  # type: ignore[method-assign]
        options = await webauthn.registration_options("u1", "ana")
        answer = authenticator.create(options)
        await refused("algorithm", webauthn.verify_registration("u1", answer))

    def test_a_credential_id_too_long(self) -> None:
        authenticator = Authenticator()
        authenticator.credential_id = b"\x01" * 1024
        with pytest.raises(WebAuthnRefused, match="credential id too long"):
            webauthn_module._authenticator_data(authenticator.data(True))

    async def test_a_raw_id_that_isnt_the_id(self) -> None:
        webauthn = make()
        authenticator = Authenticator()
        passkey = await registered(webauthn, authenticator)
        options = await webauthn.authentication_options()
        answer = authenticator.get(options, passkey.user_handle, rawId=b64(b"another"))
        await refused("malformed", webauthn.verify_authentication(answer, passkey))


class TestCBOR:
    @pytest.mark.parametrize(
        "data",
        [
            b"\xc1\x00",  # a tag
            b"\xfb" + b"\0" * 8,  # a float
            b"\x5f",  # an indefinite byte string
            b"\x01\x02",  # bytes after the item
            b"\xa2\x01\x01\x01\x02",  # a duplicate key
            b"\xa1\xf5\x01",  # a key that's true
            b"\x59\xff\xff",  # longer than it is
            b"\x9b" + b"\xff" * 8,  # an array of 2⁶⁴ items
            b"\x81" * 20 + b"\x00",  # nested too deep
            b"\x62\xff\xfe",  # text that isn't UTF-8
            b"",
        ],
    )
    def test_what_isnt_read(self, data: bytes) -> None:
        with pytest.raises(WebAuthnRefused) as caught:
            webauthn_module._cbor(data)
        assert caught.value.reason == "malformed"

    def test_what_is(self) -> None:
        value = {1: -7, "a": [b"\x00", "text", True, False, None], -1: 2**40}
        assert webauthn_module._cbor(cbor(value)) == value

    @pytest.mark.parametrize(
        "extra",
        [b"\x00", cbor([1])],  # bytes after it, extensions without the ED flag
    )
    def test_authenticator_data_with_more(self, extra: bytes) -> None:
        with pytest.raises(WebAuthnRefused, match="bytes after"):
            webauthn_module._authenticator_data(Authenticator().data(False, extra=extra))

    def test_extensions(self) -> None:
        data = Authenticator().data(False, flags=0x05 | 0x80, extra=cbor({"credProtect": 2}))
        assert webauthn_module._authenticator_data(data).flags & 0x80
        with pytest.raises(WebAuthnRefused, match="extensions aren't a map"):
            webauthn_module._authenticator_data(
                Authenticator().data(False, flags=0x85, extra=cbor([1]))
            )

    def test_too_short(self) -> None:
        with pytest.raises(WebAuthnRefused, match="too short"):
            webauthn_module._authenticator_data(b"\0" * 36)


class TestConfiguration:
    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"rp_id": "https://example.com"}, "rp_id"),
            ({"rp_id": "Example.com"}, "rp_id"),
            ({"origins": "https://example.com"}, "origins"),
            ({"origins": ["http://example.com"]}, "origin is https"),
            ({"origins": ["https://example.org"]}, "isn't on the RP ID"),
            ({"origins": ["https://notexample.com"]}, "isn't on the RP ID"),
            ({"origins": ["https://example.com/login"]}, "origin is https"),
            ({"algorithms": [-35]}, "algorithms"),
            ({"user_verification": "always"}, "user_verification"),
            ({"timeout": 5}, "timeout"),
        ],
    )
    def test_invalid(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            make(**options)

    def test_subdomains_and_localhost(self) -> None:
        make(origins=["https://login.example.com", ORIGIN])
        make(rp_id="localhost", origins=["http://localhost:8000"])

    async def test_ids_and_handles(self) -> None:
        webauthn = make()
        with pytest.raises(TypeError, match="user_id is an ID"):
            await webauthn.authentication_options(True)
        with pytest.raises(ValueError, match="1 to 64 bytes"):
            await webauthn.registration_options("u1", "ana", user_handle=b64(b"x" * 65))
        with pytest.raises(WebAuthnRefused):
            credential_id({})


async def test_the_audit_trail_and_the_metrics() -> None:
    sink = MemoryAuditSink()
    webauthn = make(audit=FastAPIAudit(sinks=[sink]))
    authenticator = Authenticator()
    passkey = await registered(webauthn, authenticator)
    options = await webauthn.authentication_options()
    answer = authenticator.get(options, passkey.user_handle)
    await webauthn.verify_authentication(answer, passkey, user_id="u1")
    await refused("challenge", webauthn.verify_authentication(answer, passkey, user_id="u1"))
    assert [(e.action, e.outcome, e.actor, e.reason) for e in sink.events] == [
        ("auth.passkey.register", "success", "u1", None),
        ("auth.passkey", "success", "u1", None),
        ("auth.passkey", "failure", "u1", "challenge"),
    ]
