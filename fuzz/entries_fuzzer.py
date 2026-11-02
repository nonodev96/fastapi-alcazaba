"""Response cache entries: whatever bytes the backend returns, the parser gives a bare value
(None), an entry, or a ValueError; an entry never brings back a cookie or a hop-by-hop header,
always rebuilds into a response whose Content-Length is its body's, and reads back the same once
written again."""

import sys
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer, and instrumenting
# every library imported takes longer than a short run lasts.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.response_cache.entries import (
        MAGIC,
        NEVER_STORED,
        VERSION,
        build_response,
        encode_entry,
        parse_entry,
    )


def _check(data: bytes) -> None:
    try:
        entry = parse_entry(data)
    except ValueError:
        return
    if entry is None:
        assert not data.startswith(MAGIC)
        return
    assert not {name.lower() for name, _ in entry.headers} & NEVER_STORED, entry.headers
    response = build_response(entry)
    assert response.headers["content-length"] == str(len(entry.payload))
    assert parse_entry(encode_entry(entry)) == entry


def fuzz_one_input(data: bytes) -> None:
    _check(data)
    # Past the magic, where the metadata is read: random bytes rarely start with it.
    _check(MAGIC + VERSION + b"\n" + data)


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
