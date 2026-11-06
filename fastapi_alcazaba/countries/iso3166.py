"""The countries of ISO 3166-1, found by any of their codes, and a Pydantic type for a country
code. Alpha-2 is the code RFC 5646 (language tags: `es-ES`) and RFC 1591 (country domains) use."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated

from pydantic import AfterValidator, WithJsonSchema

from fastapi_alcazaba.countries._iso3166 import COUNTRIES as _ROWS

# A regional indicator symbol per letter: two of them make the flag's emoji.
_REGIONAL_A = 0x1F1E6


class UnknownCountry(LookupError, ValueError):
    """No country has that code. A `ValueError` too: raised in a Pydantic validator, it's a 422.
    The message doesn't hold the code."""


@dataclass(frozen=True, slots=True)
class Country:
    """A country (or territory) of ISO 3166-1."""

    #: Two letters, `ES`: the code of language tags, country domains and most APIs.
    alpha_2: str
    #: Three letters, `ESP`.
    alpha_3: str
    #: Three digits, with their zeros: `724`, Afghanistan's `004`.
    numeric: str
    #: ISO's English short name: `Spain`.
    name: str
    #: ISO's English official name, when it has one: `Kingdom of Spain`.
    official_name: str | None = None

    @property
    def flag(self) -> str:
        """The flag's emoji, made of the two letters' regional indicators: 🇪🇸."""
        return "".join(chr(_REGIONAL_A + ord(letter) - ord("A")) for letter in self.alpha_2)


#: Every officially assigned country, by alpha-2 code, in the codes' order.
COUNTRIES: Mapping[str, Country] = MappingProxyType(
    {row[0]: Country(*row) for row in _ROWS},
)
_BY_ALPHA_3 = {country.alpha_3: country for country in COUNTRIES.values()}
_BY_NUMERIC = {country.numeric: country for country in COUNTRIES.values()}


def country(code: str | int) -> Country:
    """The country with that alpha-2 (`ES`), alpha-3 (`ESP`) or numeric (`724`, or the number
    724) code, in any case and with spaces around.

    :raises UnknownCountry: if no country has it, or it's none of those.
    """
    if isinstance(code, int) and not isinstance(code, bool):
        found = _BY_NUMERIC.get(f"{code:03d}") if 0 <= code <= 999 else None
    elif isinstance(code, str) and len(code) <= 8:  # bounded before any work
        text = code.strip(" ")
        if not text.isascii():  # before uppercasing: "ß".upper() is "SS", South Sudan's code
            found = None
        elif text.isdigit():
            found = _BY_NUMERIC.get(text.zfill(3)) if len(text) <= 3 else None
        elif len(text) == 2:
            found = COUNTRIES.get(text.upper())
        else:
            found = _BY_ALPHA_3.get(text.upper()) if len(text) == 3 else None
    else:
        found = None
    if found is None:
        raise UnknownCountry("not an ISO 3166-1 country code")
    return found


def _alpha_2(value: str) -> str:
    # ASCII before uppercasing, as in country().
    code = value.strip(" ") if len(value) <= 8 else ""
    if not (code.isascii() and code.upper() in COUNTRIES):
        raise ValueError("not an ISO 3166-1 alpha-2 country code")
    return code.upper()


#: A field that takes an ISO 3166-1 alpha-2 code (`es`, ` ES `) and gives it in uppercase (`ES`).
CountryCode = Annotated[
    str,
    AfterValidator(_alpha_2),
    WithJsonSchema({"type": "string", "pattern": "^[A-Za-z]{2}$", "examples": ["ES"]}),
]
