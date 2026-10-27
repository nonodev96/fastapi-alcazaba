"""Uploads: the type sniffed from any bytes is a known one or None, an image's size is two
integers or None, and a client's file name always comes out safe and within its length."""

import sys
import unicodedata
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer, and instrumenting
# every library imported (FastAPI, pydantic...) takes longer than a short run lasts.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.uploads import (
        _WINDOWS_RESERVED,
        KNOWN_TYPES,
        image_size,
        safe_filename,
        sniff,
    )

_FORBIDDEN = frozenset('/\\<>:"|?*')


def fuzz_one_input(data: bytes) -> None:
    kind = sniff(data)
    assert kind is None or kind in KNOWN_TYPES, kind
    size = image_size(data)
    if size is not None:
        width, height = size
        assert type(width) is int and type(height) is int and width >= 0 and height >= 0, size

    max_length = 32 + (data[0] if data else 0)
    name = safe_filename(data[1:].decode("utf-8", errors="replace"), max_length=max_length)
    assert 0 < len(name.encode()) <= max_length, (name, max_length)
    assert not _FORBIDDEN & set(name), name
    for char in name:
        category = unicodedata.category(char)
        assert category[0] != "C" and category not in ("Zl", "Zp"), name
    assert name[0] not in ". -" and name[-1] not in ". ", name
    assert not _WINDOWS_RESERVED.fullmatch(name.split(".", 1)[0].rstrip(" ")), name


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
