# pylint: disable=protected-access  # tests look at the internals they check

import warnings

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba import encryption as encryption_module
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.coder import PickleCoder
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.encryption import Encrypter
from fastapi_alcazaba.integrity import Signer

KEY = "e" * 32
OTHER_KEY = "o" * 32
CONFIDENTIAL = "The patient's diagnosis"


class TestEncrypter:
    def test_round_trip(self) -> None:
        encrypter = Encrypter(KEY)
        sealed = encrypter.seal("key", CONFIDENTIAL.encode())
        assert CONFIDENTIAL.encode() not in sealed
        assert sealed.startswith(b"fastapi_alcazaba/e1")
        assert encrypter.open("key", sealed) == CONFIDENTIAL.encode()

    def test_each_entry_is_encrypted_differently(self) -> None:
        encrypter = Encrypter(KEY)
        assert encrypter.seal("key", b"same") != encrypter.seal("key", b"same")

    def test_entries_only_open_under_their_key(self) -> None:
        encrypter = Encrypter(KEY)
        sealed = encrypter.seal("alice", b"data")
        assert encrypter.open("bob", sealed) is None

    def test_another_secret_cannot_open_them(self) -> None:
        assert Encrypter(OTHER_KEY).open("key", Encrypter(KEY).seal("key", b"data")) is None

    def test_rotation(self) -> None:
        old = Encrypter(OTHER_KEY).seal("key", b"data")
        rotated = Encrypter([KEY, OTHER_KEY])
        assert rotated.open("key", old) == b"data"
        assert Encrypter(KEY).open("key", rotated.seal("key", b"new")) == b"new"

    @pytest.mark.parametrize(
        "data", [b"", b"fastapi_alcazaba/e1", b"plain data", Signer(KEY).sign("key", b"x")]
    )
    def test_garbage_and_signed_entries_are_rejected(self, data: bytes) -> None:
        assert Encrypter(KEY).open("key", data) is None

    def test_short_secrets_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 32 bytes"):
            Encrypter("short")

    def test_needs_cryptography(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(encryption_module, "AESGCM", None)
        with pytest.raises(ImportError, match=r"fastapi-alcazaba\[crypto\]"):
            Encrypter(KEY)


@given(st.binary(), st.text(), st.integers(min_value=0), st.integers(min_value=1, max_value=255))
def test_any_tampering_is_detected(payload: bytes, key: str, position: int, flip: int) -> None:
    encrypter = Encrypter(KEY)
    sealed = bytearray(encrypter.seal(key, payload))
    sealed[position % len(sealed)] ^= flip
    assert encrypter.open(key, bytes(sealed)) is None


class TestEncryptedCache:
    async def test_the_backend_only_sees_ciphertext(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, encryption_key=KEY)
        calls: list[int] = []

        @cache(expire=60)
        async def record() -> dict[str, str]:
            calls.append(1)
            return {"diagnosis": CONFIDENTIAL}

        assert await record() == {"diagnosis": CONFIDENTIAL}
        assert await record() == {"diagnosis": CONFIDENTIAL}
        assert calls == [1]
        (stored,) = (value.data for value in backend._store.values())
        assert CONFIDENTIAL.encode() not in stored
        assert stored.startswith(b"fastapi_alcazaba/e1")

    async def test_tampered_entries_are_recomputed(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, encryption_key=KEY)
        calls: list[int] = []

        @cache(expire=60)
        async def value() -> int:
            calls.append(1)
            return 1

        await value()
        for entry in backend._store.values():
            entry.data = entry.data[:-1] + bytes([entry.data[-1] ^ 1])
        assert await value() == 1
        assert len(calls) == 2

    async def test_signed_entries_are_not_trusted_after_switching(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, signing_key=KEY)

        @cache(expire=60)
        async def value() -> int:
            return 1

        await value()
        FastAPICache.reset()
        FastAPICache.init(backend, encryption_key=KEY)
        assert await value() == 1
        (stored,) = (entry.data for entry in backend._store.values())
        assert stored.startswith(b"fastapi_alcazaba/e1")  # recomputed and stored encrypted

    def test_pickle_is_safe_with_encryption(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            FastAPICache.init(InMemoryBackend(), coder=PickleCoder, encryption_key=KEY)

    def test_one_key_or_the_other(self) -> None:
        with pytest.raises(ValueError, match="only one key"):
            FastAPICache.init(InMemoryBackend(), signing_key=KEY, encryption_key=OTHER_KEY)
