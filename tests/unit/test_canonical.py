"""canonical_json: the JSON Canonicalization Scheme of RFC 8785, checked with the RFC's own test
vectors and, where node is installed, against ECMAScript's JSON.stringify."""

import enum
import json
import math
import random
import shutil
import struct
import subprocess
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fastapi_alcazaba.core import canonical_json
from fastapi_alcazaba.core.canonical import MAX_SAFE_INTEGER, _number

BACKSLASH, QUOTE = chr(92), chr(34)

# RFC 8785, appendix B: IEEE 754 bit patterns and how ECMAScript writes them.
NUMBERS = {
    "0000000000000000": "0",
    "8000000000000000": "0",
    "0000000000000001": "5e-324",
    "8000000000000001": "-5e-324",
    "7fefffffffffffff": "1.7976931348623157e+308",
    "ffefffffffffffff": "-1.7976931348623157e+308",
    "4340000000000000": "9007199254740992",
    "c340000000000000": "-9007199254740992",
    "4430000000000000": "295147905179352830000",
    "44b52d02c7e14af5": "9.999999999999997e+22",
    "44b52d02c7e14af6": "1e+23",
    "44b52d02c7e14af7": "1.0000000000000001e+23",
    "444b1ae4d6e2ef4e": "999999999999999700000",
    "444b1ae4d6e2ef4f": "999999999999999900000",
    "444b1ae4d6e2ef50": "1e+21",
    "3eb0c6f7a0b5ed8c": "9.999999999999997e-7",
    "3eb0c6f7a0b5ed8d": "0.000001",
    "41b3de4355555553": "333333333.3333332",
    "41b3de4355555554": "333333333.33333325",
    "41b3de4355555555": "333333333.3333333",
    "41b3de4355555556": "333333333.3333334",
    "41b3de4355555557": "333333333.33333343",
    "becbf647612f3696": "-0.0000033333333333333333",
    "43143ff3c1cb0959": "1424953923781206.2",
}


def double(bits: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(bits))[0]
    return value


@pytest.mark.parametrize(("bits", "expected"), NUMBERS.items())
def test_the_rfc_s_numbers(bits: str, expected: str) -> None:
    assert canonical_json(double(bits)) == expected.encode()


def test_the_rfc_s_example() -> None:
    # Section 3.2.4: numbers written in other ways, and a string of a euro sign, "$", U+000F,
    # a line feed, "A'B", a quote, two backslashes, a quote and "/".
    text = chr(0x20AC) + "$" + chr(0x0F) + "\nA'B" + QUOTE + BACKSLASH + BACKSLASH + QUOTE + "/"
    value = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
        "string": text,
        "literals": [None, True, False],
    }
    escaped = (
        chr(0x20AC)
        + "$"
        + BACKSLASH
        + "u000f"
        + BACKSLASH
        + "nA'B"
        + BACKSLASH
        + QUOTE
        + BACKSLASH * 4
        + BACKSLASH
        + QUOTE
        + "/"
    )
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        f'"string":"{escaped}"}}'
    )
    assert canonical_json(value) == expected.encode()


def test_members_sorted_by_utf16_code_units() -> None:
    # Section 3.2.3: an emoji (D83D DE00 in UTF-16) sorts before U+FB33, though its code point
    # (U+1F600) is higher.
    names = [chr(0x20AC), "\r", chr(0xFB33), "1", chr(0x1F600), chr(0x80), chr(0xF6)]
    order = list(json.loads(canonical_json(dict.fromkeys(names, 0))))
    assert order == ["\r", "1", chr(0x80), chr(0xF6), chr(0x20AC), chr(0x1F600), chr(0xFB33)]


def test_strings_escape_only_what_json_requires() -> None:
    controls = "".join(chr(code) for code in range(0x20))
    written = json.loads(canonical_json(controls + chr(0x7F) + chr(0x2028) + "/"))
    assert written == controls + chr(0x7F) + chr(0x2028) + "/"
    raw = canonical_json("\b\t\n\f\r" + chr(1) + chr(0x1F) + chr(0x7F))
    short = "".join(BACKSLASH + letter for letter in "btnfr")
    long = BACKSLASH + "u0001" + BACKSLASH + "u001f"
    assert raw == (QUOTE + short + long + chr(0x7F) + QUOTE).encode()
    assert canonical_json(chr(0xE9)) == QUOTE.encode() + chr(0xE9).encode() + QUOTE.encode()


class Size(enum.IntEnum):
    LARGE = 3


def test_values() -> None:
    assert canonical_json({"b": [1, 2.50, 1e21], "a": None}) == b'{"a":null,"b":[1,2.5,1e+21]}'
    assert canonical_json([True, False, (1, -0.0), Size.LARGE, {}]) == b"[true,false,[1,0],3,{}]"
    assert canonical_json(MAX_SAFE_INTEGER) == str(MAX_SAFE_INTEGER).encode()
    assert canonical_json(100.0) == b"100"
    # Integers as the doubles they are, as a JavaScript parser would read them; and the way a
    # double is written, which Python reads back as an integer.
    assert canonical_json(55115876867431340) == b"55115876867431340"
    assert (
        canonical_json([2**53, 10**21, -(10**20)])
        == b"[9007199254740992,1e+21,-100000000000000000000]"
    )


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (math.nan, ValueError),
        (math.inf, ValueError),
        ([-math.inf], ValueError),
        (2**53 + 1, ValueError),  # no double is exactly it
        (10**400, ValueError),
        (chr(0xD800), ValueError),  # a lone surrogate
        ({1: "a"}, TypeError),
        (b"bytes", TypeError),
        ({"a": object()}, TypeError),
    ],
)
def test_what_isn_t_i_json(value: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        canonical_json(value)


texts = st.text(st.characters(exclude_categories=["Cs"]))  # no lone surrogates
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)
    | st.floats(allow_nan=False, allow_infinity=False)
    | texts,
    lambda children: st.lists(children) | st.dictionaries(texts, children),
    max_leaves=20,
)


@given(json_values)
def test_what_it_writes_reads_back_the_same(value: Any) -> None:
    written = canonical_json(value)
    # Numbers read as JavaScript reads them: 55115876867431340 is the double 5.511587686743134e16.
    assert json.loads(written, parse_int=float) == value
    assert canonical_json(json.loads(written)) == written  # Python's reading of it too


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_numbers_as_ecmascript_writes_them() -> None:
    rng = random.Random(8785)  # noqa: S311  # test data, not a secret
    values = [double(f"{rng.getrandbits(64):016x}") for _ in range(5000)]
    values = [value for value in values if math.isfinite(value)]
    values += [rng.uniform(-1e6, 1e6) for _ in range(1000)]
    script = (
        "const values = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "process.stdout.write(JSON.stringify(values.map((value) => JSON.stringify(value))));"
    )
    node = subprocess.run(  # noqa: S603
        ["node", "-e", script],  # noqa: S607
        input=json.dumps(values),
        capture_output=True,
        text=True,
        check=True,
    )
    assert [_number(value) for value in values] == json.loads(node.stdout)
