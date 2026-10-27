"""The JSON Canonicalization Scheme (JCS, RFC 8785): the one way of writing a JSON value, the same
in every language that implements it, so that a signature or a hash of it can be checked anywhere.

    canonical_json({"b": [1, 2.50, 1e21], "a": None})  # b'{"a":null,"b":[1,2.5,1e+21]}'

- Object members sorted by their names' UTF-16 code units, no whitespace.
- Strings in UTF-8, escaping only what JSON requires: `"`, `\\` and the control characters.
- Numbers as ECMAScript writes them: the shortest digits that read back the same, `1e+21`
  beyond 21 digits, `1e-7` below 0.000001, no `.0`.

Values must be I-JSON (RFC 7493), which every parser reads back the same: numbers that an IEEE
754 double holds exactly (every integer within ±(2⁵³ − 1), some beyond), no NaN nor infinities,
strings without lone surrogates. Integers are written as the double they are.
"""

import math
import re
from collections.abc import Mapping
from typing import Any

#: The largest integer every JSON parser keeps exactly (they read numbers as IEEE 754 doubles).
MAX_SAFE_INTEGER = 2**53 - 1

_ESCAPED = re.compile(r'["\\\x00-\x1f]')
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def canonical_json(value: Any) -> bytes:
    """`value` (dicts with string keys, lists, tuples, strings, integers, floats, booleans and
    None) in its canonical form, as UTF-8. Raises ValueError for what I-JSON leaves out (integers
    a double can't hold exactly, NaN, infinities, lone surrogates) and TypeError for what isn't
    JSON."""
    parts: list[str] = []
    _write(value, parts)
    return "".join(parts).encode()  # a lone surrogate raises UnicodeEncodeError, a ValueError


def _write(value: Any, parts: list[str]) -> None:
    if value is None:
        parts.append("null")
    elif value is True or value is False:
        parts.append("true" if value else "false")
    elif isinstance(value, str):
        parts.append(_string(value))
    elif isinstance(value, int):
        # As the double every parser reads it as: when it's one exactly (2**53, unlike 2**53 + 1),
        # or when it's how a double is written (55115876867431340, which Python reads back as an
        # integer, is the double 5.511587686743134e+16).
        number = int(value)  # an IntEnum's value
        try:
            double = float(number)
        except OverflowError:
            double = math.nan
        if double != number and not (math.isfinite(double) and _number(double) == str(number)):
            raise ValueError(f"{number} isn't a double: parsers would read another number")
        parts.append(_number(double))
    elif isinstance(value, float):
        parts.append(_number(value))
    elif isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object names are strings")
        parts.append("{")
        for index, key in enumerate(sorted(value, key=_utf16)):
            if index:
                parts.append(",")
            parts.append(_string(key))
            parts.append(":")
            _write(value[key], parts)
        parts.append("}")
    elif isinstance(value, list | tuple):
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            _write(item, parts)
        parts.append("]")
    else:
        raise TypeError(f"{type(value).__name__} isn't JSON")


def _utf16(name: str) -> bytes:
    """The sort key of a member name: its UTF-16 code units, big-endian so that bytes compare as
    the units do. U+1F600 (D83D DE00 in UTF-16) comes before U+FB33, unlike in code point order."""
    return name.encode("utf-16-be", errors="surrogatepass")


def _string(text: str) -> str:
    def escape(match: re.Match[str]) -> str:
        char = match.group()
        return _ESCAPES.get(char) or f"\\u{ord(char):04x}"

    return '"' + _ESCAPED.sub(escape, text) + '"'


def _number(value: float) -> str:
    """ECMAScript's Number::toString: the shortest digits that read back as `value` (Python's
    repr finds the same), laid out by the position of the decimal point."""
    if not math.isfinite(value):
        raise ValueError("NaN and infinities aren't JSON")
    if value == 0:
        return "0"  # -0.0 too
    if value < 0:
        return "-" + _number(-value)
    mantissa, _, exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    significant = digits.rstrip("0")
    # value = significant × 10^(point − len(significant)): `point` is where the decimal point goes.
    point = int(exponent or 0) - len(fraction) + len(digits)
    count = len(significant)
    if count <= point <= 21:
        return significant + "0" * (point - count)
    if 0 < point <= 21:
        return significant[:point] + "." + significant[point:]
    if -6 < point <= 0:
        return "0." + "0" * -point + significant
    head = significant if count == 1 else significant[0] + "." + significant[1:]
    return f"{head}e{'+' if point > 0 else '-'}{abs(point - 1)}"
