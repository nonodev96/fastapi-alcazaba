"""Refresh tokens, as clients send them: any string is a token that formats back to itself, or
None; never an exception."""

import sys
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.auth.refresh import MAX_TOKEN, format_token, parse_token


def fuzz_one_input(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")
    parsed = parse_token(text)
    if parsed is None:
        return
    assert len(text) <= MAX_TOKEN, text
    assert format_token(*parsed) == text, text


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
