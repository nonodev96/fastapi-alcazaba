"""Compressed entries: whatever a backend holds (corrupt, truncated, a decompression bomb), reading
it gives the bytes as they are, None, or no more than the limit; and what Alcazaba compresses
reads back the same."""

import sys
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer, and instrumenting
# every library imported takes longer than a short run lasts.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.response_cache.compression import Compressor, decompress

LIMIT = 4096
_MAGIC = b"\xffZ"  # compressed entries start with it, then a codec byte (1: zstd, 2: zlib)
_COMPRESSORS = [Compressor("zstd", min_size=0), Compressor("zlib", min_size=0)]


def fuzz_one_input(data: bytes) -> None:
    # As it comes, and as the body of each codec (and of an unknown one).
    for stored in (data, _MAGIC + b"\x01" + data, _MAGIC + b"\x02" + data, _MAGIC + b"\x07"):
        payload = decompress(stored, LIMIT)
        is_compressed = stored.startswith(_MAGIC) and len(stored) > len(_MAGIC)
        assert payload is None or payload is stored or len(payload) <= LIMIT, len(payload)
        assert payload is not stored or not is_compressed
    # The coders' output never starts with 0xFF (neither UTF-8 text nor a pickle does).
    if not data.startswith(b"\xff"):
        for compressor in _COMPRESSORS:
            assert decompress(compressor.compress(data)) == data


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
