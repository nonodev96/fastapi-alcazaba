"""Pagination cursors: any string a client sends is a position or a 400, signed or not, and a
position read from a cursor makes a cursor that reads back the same."""

import sys
from contextlib import nullcontext

from fastapi import HTTPException

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer, and instrumenting
# every library imported takes longer than a short run lasts.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.pagination.cursor import PaginationCursor

_SIGNED = PaginationCursor(secret="fuzz-only-secret-of-32-bytes-0123")  # noqa: S106
_UNSIGNED = PaginationCursor()


def fuzz_one_input(data: bytes) -> None:
    cursor = data.decode("utf-8", errors="replace")
    for cursors in (_UNSIGNED, _SIGNED):
        try:
            position = cursors.decode(cursor)
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
            continue
        assert cursors.decode(cursors.encode(position)) == position, position


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
