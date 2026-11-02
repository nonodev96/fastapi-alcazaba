"""The stamps memcached keeps in front of cached responses: any bytes read from the backend are
an entry without a stamp, a stamp that encodes back to the same one, or a ValueError; never another
exception."""

import sys
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.backends._generations import MAGIC, encode_stamp, parse_stamp


def fuzz_one_input(data: bytes) -> None:
    for candidate in (data, MAGIC + data):
        try:
            parsed = parse_stamp(candidate)
        except ValueError:
            continue
        if parsed is None:
            assert not candidate.startswith(MAGIC)
            continue
        stamp, value = parsed
        assert parse_stamp(encode_stamp(stamp, value)) == (stamp, value), candidate


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
