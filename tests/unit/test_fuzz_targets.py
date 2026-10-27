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

from fuzz import audit_fuzzer, compression_fuzzer, cursors_fuzzer, uploads_fuzzer, webhooks_fuzzer

TARGETS = [audit_fuzzer, compression_fuzzer, cursors_fuzzer, uploads_fuzzer, webhooks_fuzzer]


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
        # An extension short in characters but not in bytes: the name came out longer.
        (uploads_fuzzer, b"\x00" + b"a" * 100 + b"." + "\U0001f600".encode() * 15),
        (uploads_fuzzer, b"\x00" + b"CON .txt"),
        (uploads_fuzzer, b"\x89PNG\r\n\x1a\n" + b"\x00" * 16),
        # A decompression bomb: a megabyte of zeros in a thousand bytes.
        (compression_fuzzer, zlib.compress(b"\0" * 1_000_000)),
        (webhooks_fuzzer, b"msg_1\n1800000000\nv1," + base64.b64encode(b"x" * 32) + b"\n{}"),
        (
            audit_fuzzer,
            json.dumps({"password": "a", "nested": [[[[["deep"]]]]], "n": 1e308}).encode(),
        ),
        (audit_fuzzer, b'{"x": NaN, "y": -Infinity}'),
        (audit_fuzzer, b"[" * 100_000),  # too deep for json itself
    ],
)
def test_known_cases(module: ModuleType, data: bytes) -> None:
    target(module)(data)
