"""Pagination cursors: any string a client sends is a position or a 400, signed or not, and a
position read from a cursor makes a cursor that reads back the same."""

import sys

from fastapi import HTTPException

from fastapi_alcazaba.pagination import FastAPIPaginationCursor

_SIGNED = FastAPIPaginationCursor(secret="fuzz-only-secret-of-32-bytes-0123")  # noqa: S106
_UNSIGNED = FastAPIPaginationCursor()


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
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image

    atheris.instrument_all()
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
