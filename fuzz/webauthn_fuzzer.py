"""WebAuthn's parsers: whatever bytes an authenticator (or someone posing as one) sends, the CBOR
reader, the authenticator data and base64url either give what they read, within their bounds, or
refuse it with WebAuthnRefused; nothing else is raised, and nothing hangs."""

import sys
from contextlib import nullcontext
from typing import Any

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.auth.webauthn import (
        _MAX_CBOR_DEPTH,
        _MAX_CREDENTIAL_ID,
        WebAuthnRefused,
        _authenticator_data,
        _b64decode,
        _cbor,
    )


def _depth(value: Any) -> int:
    if isinstance(value, list):
        return 1 + max((_depth(item) for item in value), default=0)
    if isinstance(value, dict):
        for key in value:
            assert isinstance(key, int | str) and not isinstance(key, bool), key
        return 1 + max((_depth(item) for item in value.values()), default=0)
    assert value is None or isinstance(value, bool | int | bytes | str), type(value)
    return 0


def fuzz_one_input(data: bytes) -> None:
    try:
        value = _cbor(data)
    except WebAuthnRefused:
        pass
    else:
        assert _depth(value) <= _MAX_CBOR_DEPTH + 1
    try:
        parsed = _authenticator_data(data)
    except WebAuthnRefused:
        pass
    else:
        assert len(parsed.rp_id_hash) == 32
        if parsed.credential_id is not None:
            assert len(parsed.credential_id) <= _MAX_CREDENTIAL_ID
            assert parsed.public_key is not None
            _cbor(parsed.public_key)  # the key it kept is one CBOR item
        assert not parsed.flags & 0x10 or parsed.flags & 0x08  # backed up: eligible
    text = data.decode("latin-1")
    try:
        _b64decode(text, "fuzzed")
    except WebAuthnRefused:
        pass


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
