"""The fuzz targets of fuzz/ as plain tests: on random inputs, and on the cases that broke a
property they check. ClusterFuzzLite runs them for longer, guided by coverage."""

import base64
import json
import zlib
from collections.abc import Callable
from types import ModuleType

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fuzz import (
    audit_fuzzer,
    compression_fuzzer,
    countries_fuzzer,
    cursors_fuzzer,
    entries_fuzzer,
    passwords_fuzzer,
    refresh_fuzzer,
    sessions_fuzzer,
    stamps_fuzzer,
    uploads_fuzzer,
    webauthn_fuzzer,
    webhooks_fuzzer,
)

TARGETS = [
    audit_fuzzer,
    compression_fuzzer,
    countries_fuzzer,
    cursors_fuzzer,
    entries_fuzzer,
    passwords_fuzzer,
    refresh_fuzzer,
    sessions_fuzzer,
    stamps_fuzzer,
    uploads_fuzzer,
    webauthn_fuzzer,
    webhooks_fuzzer,
]


def target(module: ModuleType) -> Callable[[bytes], None]:
    fuzz: Callable[[bytes], None] = module.fuzz_one_input
    return fuzz


@pytest.mark.parametrize("module", TARGETS, ids=lambda module: module.__name__.rsplit(".", 1)[-1])
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.binary(max_size=4096))
def test_random_inputs(module: ModuleType, data: bytes) -> None:
    target(module)(data)


@pytest.mark.parametrize(
    ("module", "data"),
    [
        # A signed cursor that isn't ASCII: compare_digest raised a TypeError instead of a 400.
        (cursors_fuzzer, "eyJ.é".encode()),
        # JSON that json reads but can't write back: the target sealed it, and seal refused it.
        (cursors_fuzzer, b"NaN"),
        (cursors_fuzzer, b"[1e999]"),
        (cursors_fuzzer, b'{"after": -Infinity}'),
        # Cache entries: metadata that isn't an object, a bool for a status, forbidden and
        # malformed headers, a value outside latin-1, too deep for json.
        (entries_fuzzer, b"[]\n"),
        (entries_fuzzer, b'{"kind": "response", "status": true, "headers": []}\nbody'),
        (
            entries_fuzzer,
            b'{"kind": "value", "status": 200, "headers": [["Set-Cookie", "s=1"], ["a b", "x"],'
            b' ["x", "\\r\\n"], ["x", "\\u20ac"], [1, 2]]}\n',
        ),
        (entries_fuzzer, b"[" * 100_000),
        # An extension short in characters but not in bytes: the name came out longer.
        (uploads_fuzzer, b"\x00" + b"a" * 100 + b"." + "\U0001f600".encode() * 15),
        (uploads_fuzzer, b"\x00" + b"CON .txt"),
        (uploads_fuzzer, b"\x89PNG\r\n\x1a\n" + b"\x00" * 16),
        # A decompression bomb: a megabyte of zeros in a thousand bytes.
        (compression_fuzzer, zlib.compress(b"\0" * 1_000_000)),
        # Webhooks: the first byte picks the scheme (github, slack, standard, stripe).
        (webhooks_fuzzer, b"\x02msg_1\n1800000000\nv1," + base64.b64encode(b"x" * 32) + b"\n{}"),
        (webhooks_fuzzer, b"\x00d1\nsha256=" + b"0" * 64 + b"\n{}"),
        (webhooks_fuzzer, b"\x03t=1800000000,v1=" + b"0" * 64 + b"\n{}"),
        (webhooks_fuzzer, b"\x01" + b"9" * 13 + b"\nv0=" + b"0" * 64 + b"\nbody"),
        (webhooks_fuzzer, b""),
        (
            audit_fuzzer,
            json.dumps({"password": "a", "nested": [[[[["deep"]]]]], "n": 1e308}).encode(),
        ),
        (audit_fuzzer, b'{"x": NaN, "y": -Infinity}'),
        (audit_fuzzer, b"[" * 100_000),  # too deep for json itself
        # CBOR nested past the limit, a length past the end, a map of a 2^64 entries.
        (webauthn_fuzzer, b"\x81" * 100 + b"\x00"),
        (webauthn_fuzzer, b"\x5b" + b"\xff" * 8),
        (webauthn_fuzzer, b"\xbb" + b"\xff" * 8),
        # Authenticator data with AT and a credential id longer than the data.
        (webauthn_fuzzer, b"\x00" * 32 + b"\x41" + b"\x00" * 4 + b"\x00" * 16 + b"\xff\xff"),
        (webauthn_fuzzer, b"\x00" * 32 + b"\x10" + b"\x00" * 4),  # backed up, not eligible
        # Sessions: not JSON, the wrong types, a bool for a time, an old record, in the future.
        (sessions_fuzzer, b"\xff\xfe"),
        (sessions_fuzzer, b'{"data": {}, "created": true, "seen": 1}'),
        (sessions_fuzzer, b'{"data": {}, "created": 1800000000, "seen": 1800000000}'),
        (sessions_fuzzer, b'{"data": [], "created": 1, "seen": 1, "started": 1}'),
        (sessions_fuzzer, b'["data"]'),
        (sessions_fuzzer, b'{"data": {"user_id": "ana"}, "created": 1800000000, "seen": 1}'),
        # Refresh tokens: a whole one, a leading zero, a newline after it, Unicode digits.
        (refresh_fuzzer, b"r1." + b"a" * 22 + b".7." + b"b" * 43),
        (refresh_fuzzer, b"r1." + b"a" * 22 + b".07." + b"b" * 43),
        (refresh_fuzzer, b"r1." + b"a" * 22 + b".7." + b"b" * 43 + b"\n"),
        (refresh_fuzzer, b"r1." + b"a" * 22 + ".\u0667.".encode() + b"b" * 43),
        # Stamps: a whole one, none, no end of line, too deep, not UTF-8, a float, a bool expiry.
        (stamps_fuzzer, b'{"e": 1800000000, "g": {"a": "1"}}\nvalue'),
        (stamps_fuzzer, b'{"written": "by 0.7"}'),
        (stamps_fuzzer, b'{"e": 0, "g": {}}'),
        (stamps_fuzzer, b"[" * 100_000 + b"\n"),
        (stamps_fuzzer, b"\xff\n"),
        (stamps_fuzzer, b'{"e": 1e999, "g": {}}\n'),
        (stamps_fuzzer, b'{"e": false, "g": {}}\n'),
        # Countries and Spanish IDs: "ß" is "SS" (South Sudan) once uppercased, Unicode digits,
        # a NIE with no digits, each kind, a postal code with spaces, a province by ISO code.
        (countries_fuzzer, "ß".encode()),
        (countries_fuzzer, "\u0667\u0662\u0664".encode()),
        (countries_fuzzer, b" 004 "),
        (countries_fuzzer, b"XT"),
        (countries_fuzzer, b"12.345.678-z"),
        (countries_fuzzer, b"x-123456-s"),
        (countries_fuzzer, b"K0123456S"),
        (countries_fuzzer, b"G0012345E"),
        (countries_fuzzer, b" 18001 "),
        (countries_fuzzer, b"es-gr"),
        (countries_fuzzer, b"-" * 21),
        # Passwords: a range answer, a count past int()'s 4300 digits, digits of another
        # script, a lone surrogate, a fullwidth blocklisted one, a megabyte.
        (passwords_fuzzer, b"0" * 35 + b":42\r\n" + b"F" * 35 + b":0"),
        (passwords_fuzzer, b"0" * 35 + b":" + b"9" * 5000),
        (passwords_fuzzer, b"0" * 35 + b":" + "\u0664\u0662".encode()),
        (passwords_fuzzer, b"a long password \xed\xa0\x80\nana"),
        (passwords_fuzzer, "ＡＣＭＥ２０２４".encode()),
        (passwords_fuzzer, b"ana@example.com" * 70_000 + b"\nana@example.com"),
    ],
)
def test_known_cases(module: ModuleType, data: bytes) -> None:
    target(module)(data)
