"""Compression of large entries: zstd (standard library from Python 3.14, the `zstd` extra
before) or zlib.

A compressed entry is b"\\xffZ", a codec byte and the compressed data. 0xFF can't start UTF-8
text (JSON) nor a pickle, so uncompressed entries are recognised as they are. Decompression is
bounded: an entry that would grow past the limit (a decompression bomb planted in an unprotected
backend) is rejected, not inflated.
"""

import functools
import zlib
from types import ModuleType
from typing import Literal

Codec = Literal["zstd", "zlib"]

_MAGIC = b"\xffZ"
_CODEC_IDS: dict[Codec, int] = {"zstd": 1, "zlib": 2}
#: Largest entry decompressed when FastAPICache.init(max_value_size=...) isn't set.
MAX_DECOMPRESSED_SIZE = 64 * 1024 * 1024


@functools.cache
def _load_zstd() -> ModuleType | None:
    """Looked up once: a failed import walks sys.path again on every attempt."""
    try:
        from compression import zstd  # type: ignore[import-not-found,unused-ignore]  # 3.14+
    except ImportError:
        try:
            from backports import zstd  # type: ignore[import-not-found,no-redef,unused-ignore]
        except ImportError:
            return None
    module: ModuleType = zstd
    return module


def _zstd() -> ModuleType:
    module = _load_zstd()
    if module is None:
        raise ImportError(
            "zstd compression needs Python 3.14 or the backports.zstd package: "
            "pip install 'fastapi-alcazaba[zstd]'"
        )
    return module


class Compressor:
    """Compresses payloads of at least `min_size` bytes, when that makes them smaller."""

    def __init__(self, codec: Codec = "zstd", *, min_size: int = 1024, level: int | None = None):
        if codec not in _CODEC_IDS:
            raise ValueError(f"unknown compression codec {codec!r}: use 'zstd' or 'zlib'")
        if min_size < 0:
            raise ValueError("compression_min_size can't be negative")
        self.codec = codec
        self.min_size = min_size
        self.level = level
        self._zstd = _zstd() if codec == "zstd" else None

    def compress(self, payload: bytes) -> bytes:
        if len(payload) < self.min_size:
            return payload
        if self._zstd is not None:
            compressed: bytes = self._zstd.compress(payload, level=self.level)
        else:
            compressed = zlib.compress(payload, -1 if self.level is None else self.level)
        if len(compressed) + len(_MAGIC) + 1 >= len(payload):
            return payload  # not worth it (already compressed data, random bytes...)
        return _MAGIC + bytes([_CODEC_IDS[self.codec]]) + compressed


def decompress(data: bytes, limit: int = MAX_DECOMPRESSED_SIZE) -> bytes | None:
    """The original payload: `data` itself when it isn't compressed, None when it's corrupt, its
    codec unavailable, or it would decompress to more than `limit` bytes."""
    if not data.startswith(_MAGIC) or len(data) < len(_MAGIC) + 1:
        return data
    codec_id, body = data[len(_MAGIC)], data[len(_MAGIC) + 1 :]
    try:
        if codec_id == _CODEC_IDS["zstd"]:
            decompressor = _zstd().ZstdDecompressor()
            output: bytes = decompressor.decompress(body, max_length=limit)
            complete = decompressor.eof and not decompressor.unused_data
        elif codec_id == _CODEC_IDS["zlib"]:
            inflater = zlib.decompressobj()
            output = inflater.decompress(body, limit)
            complete = inflater.eof and not inflater.unconsumed_tail and not inflater.unused_data
        else:
            return None
    except Exception:  # corrupt data, or the codec isn't available
        return None
    return output if complete else None
