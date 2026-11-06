"""Spanish tax IDs (NIF): a person's DNI, NIE or NIF K/L/M, and a legal entity's NIF (the CIF
before 2008), normalized and checked by their control character.

The rules are the Ministry of the Interior's for the DNI and the NIE, and Orden EHA/451/2008's
for the others.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

#: The longest value taken, separators included: 9 characters, and a few spaces, hyphens, dots.
MAX_LENGTH = 20

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_CIF_LETTERS = "JABCDEFGHI"
_NIE_PREFIXES = {"X": "0", "Y": "1", "Z": "2"}
# The first letter of a legal entity's NIF: its kind (A a company, B a limited company...).
_CIF_FIRST = frozenset("ABCDEFGHJNPQRSUVW")
# Public bodies, churches, non-residents (N, W) and civil partnerships... end with a letter;
# A, B, E and H with a digit; the others with either.
_CIF_LETTER_ONLY = frozenset("NPQRSW")
_CIF_DIGIT_ONLY = frozenset("ABEH")
# Numbers that pass the check but are no one's, used in forms as placeholders.
_PLACEHOLDERS = frozenset({"00000000T", "00000001R", "99999999R"})
_SEPARATORS = str.maketrans("", "", " -.")


class SpanishIdKind(StrEnum):
    """What a Spanish tax ID belongs to."""

    #: A Spanish citizen's national ID card: 8 digits and a letter, `12345678Z`.
    DNI = "DNI"
    #: A foreigner's ID: X, Y or Z, 7 digits and a letter, `X1234567L`.
    NIE = "NIE"
    #: A person's NIF without DNI nor NIE: K (Spanish under 14), L (Spanish living abroad), M
    #: (foreigner without NIE); 7 digits and a letter, `K0123456S`.
    NIF_KLM = "NIF_KLM"
    #: A legal entity's NIF, called CIF before 2008: a letter, 7 digits and a digit or a letter,
    #: `B12345674`.
    CIF = "CIF"


#: Every kind: a NIF, of a person or a legal entity.
NIF_KINDS = frozenset(SpanishIdKind)
#: A person's: a DNI, a NIE or a NIF K/L/M.
PERSON_KINDS = frozenset({SpanishIdKind.DNI, SpanishIdKind.NIE, SpanishIdKind.NIF_KLM})


class InvalidSpanishId(ValueError):
    """Not a valid Spanish tax ID, or not of a kind accepted. The message doesn't hold the
    value: an ID is personal data."""


@dataclass(frozen=True, slots=True)
class SpanishId:
    """A valid Spanish tax ID: its value without separators, in uppercase, and its kind."""

    value: str
    kind: SpanishIdKind

    def __str__(self) -> str:
        return self.value


def _dni_letter(number: str) -> str:
    return _DNI_LETTERS[int(number) % 23]


def _cif_control(digits: str) -> int:
    """The control digit of a legal entity's 7 digits: Luhn's sum, as 10 minus its last digit."""
    total = 0
    for position, char in enumerate(digits):
        digit = int(char)
        if position % 2 == 0:  # the 1st, 3rd, 5th and 7th, doubled, their digits added
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return (10 - total % 10) % 10


def _person(text: str) -> SpanishId:
    """A DNI, NIE or NIF K/L/M: a letter of the number, padded with zeros for a DNI or a NIE."""
    first, letter = text[0], text[-1]
    if first in _NIE_PREFIXES:
        kind, digits, most = SpanishIdKind.NIE, text[1:-1], 7
    elif first in "KLM":
        kind, digits, most = SpanishIdKind.NIF_KLM, text[1:-1], 7
    else:
        kind, digits, most = SpanishIdKind.DNI, text[:-1], 8
    if not (digits.isdigit() and len(digits) <= most):
        raise InvalidSpanishId(f"not a valid {kind.value}")
    if kind is SpanishIdKind.NIF_KLM:
        if len(digits) != most:
            raise InvalidSpanishId("a NIF K, L or M has 7 digits")
        number, value = digits, text
    else:
        digits = digits.zfill(most)
        prefix = first if kind is SpanishIdKind.NIE else ""
        number, value = _NIE_PREFIXES.get(prefix, "") + digits, prefix + digits + letter
    if number + letter in _PLACEHOLDERS:
        raise InvalidSpanishId(f"a placeholder, not a {kind.value}")
    if letter != _dni_letter(number):
        raise InvalidSpanishId(f"the {kind.value}'s control letter doesn't match")
    return SpanishId(value, kind)


def _entity(text: str) -> SpanishId:
    first, digits, last = text[0], text[1:-1], text[-1]
    if len(text) != 9 or not digits.isdigit():
        raise InvalidSpanishId("a CIF is a letter, 7 digits and a control character")
    control = _cif_control(digits)
    letter_ok, digit_ok = last == _CIF_LETTERS[control], last == str(control)
    # Non-residents (number 00...) and some kinds take only the letter; others only the digit.
    if first in _CIF_LETTER_ONLY or digits.startswith("00"):
        valid = letter_ok
    elif first in _CIF_DIGIT_ONLY:
        valid = digit_ok
    else:
        valid = letter_ok or digit_ok
    if not valid:
        raise InvalidSpanishId("the CIF's control character doesn't match")
    return SpanishId(text, SpanishIdKind.CIF)


def parse_spanish_id(
    value: str,
    *,
    kinds: Iterable[SpanishIdKind] = NIF_KINDS,
) -> SpanishId:
    """The Spanish tax ID in `value`, normalized: spaces, hyphens and dots removed, in uppercase,
    a DNI's or NIE's number padded with zeros (`12.345.678-z` is `12345678Z`, `x-123456-s` is
    `X0123456S`).

    :param kinds: the kinds accepted; any by default.
    :raises InvalidSpanishId: if it isn't one, or not of a kind in `kinds`.
    """
    if not isinstance(value, str):
        raise TypeError(f"a Spanish tax ID is a str, not {type(value).__name__}")
    if len(value) > MAX_LENGTH:
        raise InvalidSpanishId("too long for a Spanish tax ID")
    text = value.translate(_SEPARATORS)
    # ASCII only: `str.isdigit` and int() take other scripts' digits too.
    if not (2 <= len(text) <= 9 and text.isascii() and text.isalnum()):
        raise InvalidSpanishId("not a Spanish tax ID")
    text = text.upper()
    if text[0].isdigit() or text[0] in "XYZKLM":
        if not text[-1].isalpha():
            raise InvalidSpanishId("not a Spanish tax ID: it must end with a letter")
        parsed = _person(text)
    elif text[0] in _CIF_FIRST:
        parsed = _entity(text)
    else:
        raise InvalidSpanishId("not a Spanish tax ID")
    accepted = frozenset(kinds)
    if parsed.kind not in accepted:
        names = ", ".join(kind.value for kind in SpanishIdKind if kind in accepted)
        raise InvalidSpanishId(f"a {parsed.kind.value} isn't accepted here, only: {names}")
    return parsed


class SpanishIdKinds:
    """A field's marker: it takes a Spanish tax ID of these kinds, and gives its normalized value.

    PersonNIF = Annotated[str, SpanishIdKinds(*PERSON_KINDS)]
    """

    __slots__ = ("kinds",)

    def __init__(self, *kinds: SpanishIdKind) -> None:
        if not kinds:
            raise ValueError("SpanishIdKinds needs at least one kind")
        self.kinds = frozenset(kinds)

    def __repr__(self) -> str:
        return f"SpanishIdKinds({', '.join(sorted(kind.value for kind in self.kinds))})"

    def _first(self) -> SpanishIdKind:
        """The first of the kinds in the enum's order (a DNI before a CIF), for the example."""
        return next(kind for kind in SpanishIdKind if kind in self.kinds)

    def _validate(self, value: str) -> str:
        return parse_spanish_id(value, kinds=self.kinds).value

    def __get_pydantic_core_schema__(
        self,
        source: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(self._validate, handler(source))

    def __get_pydantic_json_schema__(
        self,
        schema: core_schema.CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        json_schema.update(maxLength=MAX_LENGTH, examples=[_EXAMPLES[self._first()]])
        return json_schema


_EXAMPLES = {
    SpanishIdKind.CIF: "B12345674",
    SpanishIdKind.DNI: "12345678Z",
    SpanishIdKind.NIE: "X1234567L",
    SpanishIdKind.NIF_KLM: "K0123456S",
}
