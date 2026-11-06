"""Countries: any text is a country, a province, a postal code's province or a Spanish tax ID, or
the error each documents, never another exception; what's accepted is ASCII, normalized, and
parses back to itself."""

import sys
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.countries import COUNTRIES, UnknownCountry, country
    from fastapi_alcazaba.countries.spain import (
        MAX_LENGTH,
        PROVINCES,
        InvalidSpanishId,
        SpanishIdKind,
        UnknownProvince,
        parse_spanish_id,
        province,
        province_for_postal_code,
    )


def fuzz_one_input(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")

    try:
        found = country(text)
    except UnknownCountry:
        pass
    else:
        assert COUNTRIES[found.alpha_2] is found, text
        assert text.strip(" ").upper() in (found.alpha_2, found.alpha_3, found.numeric) or (
            text.strip(" ").zfill(3) == found.numeric
        ), text

    for lookup in (province, province_for_postal_code):
        try:
            assert lookup(text) in PROVINCES, text
        except UnknownProvince:
            pass

    try:
        parsed = parse_spanish_id(text)
    except InvalidSpanishId as error:
        # The messages are fixed: none holds the ID's digits (only "7 digits" has one).
        digits = "".join(char for char in text if char in "0123456789")
        assert len(digits) < 2 or digits not in str(error), (text, error)
    else:
        assert len(text) <= MAX_LENGTH, text
        value = parsed.value
        assert len(value) == 9 and value.isascii() and value.isalnum() and value.isupper(), value
        assert parsed.kind in SpanishIdKind, parsed
        assert parse_spanish_id(value) == parsed, value


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
