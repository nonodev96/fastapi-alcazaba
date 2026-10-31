"""Pagination cursors and keys: any string a client sends as a cursor, or as the `after` of a
keyset, is a position or a 400; whatever JSON a signed cursor holds too (as if the secret had
leaked); and a position read from either makes one that reads back the same."""

import json
import sys
from contextlib import nullcontext
from datetime import datetime

from fastapi import HTTPException

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer, and instrumenting
# every library imported takes longer than a short run lasts.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.pagination.cursor import PaginationCursor
    from fastapi_alcazaba.pagination.keyset import PaginationKeyset
    from fastapi_alcazaba.pagination.values import Sealer

_SECRET = "fuzz-only-secret-of-32-bytes-0123"  # noqa: S105  # fuzzing only
_CURSORS = PaginationCursor(_SECRET)
_SEALER = Sealer(_SECRET, b"fastapi_alcazaba.cursor")
_KEYSETS = [PaginationKeyset(sort=kind) for kind in (int, str, datetime, (datetime, int))]


def _cursor(text: str) -> None:
    try:
        params = _CURSORS.params(cursor=text)
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
        return
    position = params.after if params.after is not None else params.before
    assert position is not None
    again = _CURSORS.params(cursor=_CURSORS.encode(position, before=params.before is not None))
    assert (again.after, again.before) == (params.after, params.before), position


def fuzz_one_input(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")
    _cursor(text)
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        pass
    else:
        _cursor(_SEALER.seal(payload))
    for keyset in _KEYSETS:
        try:
            position = keyset.decode(text)
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
            continue
        assert keyset.decode(keyset.encode(position)) == position, position


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
