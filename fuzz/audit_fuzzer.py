"""Audit details: whatever an app passes, what's recorded is JSON (no NaN, nothing that isn't),
bounded, and without the values of keys that name a secret."""

import json
import math
import sys
from contextlib import nullcontext
from typing import Any

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer, and instrumenting
# every library imported takes longer than a short run lasts.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.audit import (
        MAX_DEPTH,
        MAX_ITEMS,
        MAX_STRING,
        REDACTED,
        SENSITIVE_WORDS,
        _Cleaner,
        _words,
    )

_CLEANER = _Cleaner(SENSITIVE_WORDS)
_LONGEST = MAX_STRING + len("…[] more") + 20  # a string cut, with how much was left out


def _check(original: Any, cleaned: Any, depth: int) -> None:
    assert depth <= MAX_DEPTH, depth
    if isinstance(cleaned, str):
        assert len(cleaned) <= _LONGEST, len(cleaned)
    elif isinstance(cleaned, float):
        assert math.isfinite(cleaned), cleaned
    elif isinstance(cleaned, list):
        assert len(cleaned) <= MAX_ITEMS + 1, len(cleaned)
        items = list(original) if isinstance(original, list) else []
        for item, kept in zip(items, cleaned, strict=False):
            _check(item, kept, depth + 1)
    elif isinstance(cleaned, dict):
        assert len(cleaned) <= MAX_ITEMS + 1, len(cleaned)
        for key, kept in cleaned.items():
            if key in original and not SENSITIVE_WORDS.isdisjoint(_words(key)):
                assert kept == REDACTED, key
            elif key in original:
                _check(original[key], kept, depth + 1)


def fuzz_one_input(data: bytes) -> None:
    try:
        value = json.loads(data)  # NaN and Infinity included
    except (ValueError, RecursionError):
        value = data.decode("utf-8", errors="replace")
    details = value if isinstance(value, dict) else {"value": value}
    cleaned = _CLEANER.details(details)
    json.dumps(cleaned, allow_nan=False)  # JSON all the way down
    _check(details, cleaned, 0)


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
