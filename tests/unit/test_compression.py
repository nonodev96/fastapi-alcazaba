# pylint: disable=protected-access  # tests look at the internals they check

import os
import sys
import zlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.core import Encrypter
from fastapi_alcazaba.response_cache import cache
from fastapi_alcazaba.response_cache import compression as compression_module
from fastapi_alcazaba.response_cache.compression import Compressor, decompress

TEXT = b'{"name": "olive oil", "price": 9.5}' * 200
CODECS = ["zstd", "zlib"]


class TestCompressor:
    @pytest.mark.parametrize("codec", CODECS)
    def test_round_trip(self, codec: str) -> None:
        compressed = Compressor(codec).compress(TEXT)  # type: ignore[arg-type]
        assert compressed.startswith(b"\xffZ")
        assert len(compressed) < len(TEXT) / 10
        assert decompress(compressed) == TEXT

    def test_small_payloads_are_left_alone(self) -> None:
        assert Compressor("zstd", min_size=1024).compress(b'{"a": 1}') == b'{"a": 1}'

    def test_min_size_is_inclusive(self) -> None:
        """Found by mutation testing: nothing checked the boundary itself."""
        compressor = Compressor("zlib", min_size=len(TEXT))
        assert compressor.compress(TEXT).startswith(b"\xffZ")
        assert compressor.compress(TEXT[:-1]) == TEXT[:-1]

    def test_kept_only_when_it_saves_space(self) -> None:
        """A compressed copy no smaller than the original (header included) isn't kept."""
        compressor = Compressor("zlib", min_size=0)
        for payload in (b"", b"a", b"ab", os.urandom(64)):
            assert compressor.compress(payload) == payload

    def test_headers_without_data(self) -> None:
        assert decompress(b"\xffZ") == b"\xffZ"  # too short to be ours: left alone
        assert decompress(b"\xffZ\x01") is None  # ours, but empty: corrupt
        assert decompress(b"\xffZ\x02") is None

    def test_incompressible_payloads_are_left_alone(self) -> None:
        noise = os.urandom(4096)
        assert Compressor("zstd").compress(noise) == noise

    def test_uncompressed_data_passes_through(self) -> None:
        assert decompress(TEXT) == TEXT
        assert decompress(b"\x80\x05pickle") == b"\x80\x05pickle"

    @pytest.mark.parametrize(
        "data",
        [
            b"\xffZ\x01not zstd",
            b"\xffZ\x02not zlib",
            b"\xffZ\x09unknown codec",
            Compressor("zstd").compress(TEXT)[:-10],  # truncated
            Compressor("zlib").compress(TEXT)[:-10],
        ],
    )
    def test_corrupt_entries_are_rejected(self, data: bytes) -> None:
        assert decompress(data) is None

    @pytest.mark.parametrize("codec", CODECS)
    def test_decompression_bombs_are_not_inflated(self, codec: str) -> None:
        bomb = Compressor(codec).compress(bytes(10 * 1024 * 1024))  # type: ignore[arg-type]
        assert len(bomb) < 20_000
        assert decompress(bomb, limit=1024 * 1024) is None
        assert decompress(bomb, limit=11 * 1024 * 1024) == bytes(10 * 1024 * 1024)

    def test_levels_are_applied(self) -> None:
        """Found by mutation testing: nothing checked that `level` reached the codec."""
        assert Compressor("zlib", level=1).compress(TEXT)[3:] == zlib.compress(TEXT, 1)
        assert Compressor("zlib").compress(TEXT)[3:] == zlib.compress(TEXT, -1)
        zstd = compression_module._zstd()
        assert Compressor("zstd", level=19).compress(TEXT)[3:] == zstd.compress(TEXT, level=19)
        assert Compressor("zstd", level=19).compress(TEXT) != Compressor("zstd").compress(TEXT)

    def test_invalid_settings(self) -> None:
        with pytest.raises(ValueError, match="codec"):
            Compressor("brotli")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="negative"):
            Compressor("zlib", min_size=-1)

    def test_zstd_needs_python_314_or_the_backport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for module in ("compression", "compression.zstd", "backports", "backports.zstd"):
            monkeypatch.setitem(sys.modules, module, None)
        compression_module._load_zstd.cache_clear()
        try:
            with pytest.raises(ImportError, match=r"fastapi-alcazaba\[zstd\]"):
                Compressor("zstd")
        finally:
            compression_module._load_zstd.cache_clear()


@pytest.mark.parametrize("codec", CODECS)
@given(st.binary(max_size=5000))
def test_any_payload_round_trips(codec: str, payload: bytes) -> None:
    assert decompress(Compressor(codec, min_size=0).compress(payload)) == payload  # type: ignore[arg-type]


class TestCompressedCache:
    async def test_large_entries_are_stored_compressed(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, compression="zstd")

        @cache(expire=60)
        async def catalog() -> list[dict[str, object]]:
            return [{"name": f"product {i}", "price": i * 1.5} for i in range(200)]

        first = await catalog()
        assert await catalog() == first
        (stored,) = (entry.data for entry in backend._store.values())
        assert stored.startswith(b"\xffZ\x01")

    async def test_compressed_then_encrypted(self) -> None:
        backend = InMemoryBackend()
        key = "e" * 32
        FastAPICache.init(backend, compression="zlib", encryption_key=key)

        @cache(expire=60)
        async def catalog() -> list[int]:
            return list(range(1000))

        assert await catalog() == list(range(1000))
        assert await catalog() == list(range(1000))
        ((cache_key, entry),) = backend._store.items()
        assert entry.data.startswith(b"fastapi_alcazaba/e1")
        opened = Encrypter(key).open(cache_key, entry.data)
        assert opened is not None
        assert opened.startswith(b"\xffZ\x02")

    async def test_compressed_entries_are_read_after_turning_it_off(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, compression="zstd")
        calls: list[int] = []

        @cache(expire=60)
        async def catalog() -> list[int]:
            calls.append(1)
            return list(range(1000))

        await catalog()
        FastAPICache.reset()
        FastAPICache.init(backend)
        assert await catalog() == list(range(1000))
        assert calls == [1]

    async def test_a_planted_bomb_is_recomputed(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, max_value_size=1024 * 1024)
        calls: list[int] = []

        @cache(expire=60)
        async def value() -> int:
            calls.append(1)
            return 1

        await value()
        bomb = Compressor("zstd").compress(bytes(50 * 1024 * 1024))
        for entry in backend._store.values():
            entry.data = bomb
        assert await value() == 1
        assert len(calls) == 2
