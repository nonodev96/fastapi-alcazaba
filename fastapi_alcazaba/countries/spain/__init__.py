"""Spain: its provinces and autonomous communities by INE code, its postal codes, and its tax IDs
(DNI, NIE, NIF K/L/M and a legal entity's CIF) checked by their control character, with
Pydantic types for each."""

from typing import Annotated

from fastapi_alcazaba.countries.spain.provinces import (
    COMMUNITIES,
    PROVINCES,
    Community,
    CommunityCode,
    Province,
    ProvinceCode,
    SpanishPostalCode,
    UnknownProvince,
    community,
    province,
    province_for_postal_code,
)
from fastapi_alcazaba.countries.spain.tax_ids import (
    MAX_LENGTH,
    NIF_KINDS,
    PERSON_KINDS,
    InvalidSpanishId,
    SpanishId,
    SpanishIdKind,
    SpanishIdKinds,
    parse_spanish_id,
)

#: A field that takes a DNI and gives it normalized: `12.345.678-z` is `12345678Z`.
DNI = Annotated[str, SpanishIdKinds(SpanishIdKind.DNI)]
#: A field that takes a NIE: `X-1234567-L` is `X1234567L`.
NIE = Annotated[str, SpanishIdKinds(SpanishIdKind.NIE)]
#: A field that takes a legal entity's NIF (CIF): `B-12345674` is `B12345674`.
CIF = Annotated[str, SpanishIdKinds(SpanishIdKind.CIF)]
#: A field that takes any NIF: a person's (DNI, NIE, K/L/M) or a legal entity's (CIF).
NIF = Annotated[str, SpanishIdKinds(*NIF_KINDS)]

__all__ = [
    "CIF",
    "COMMUNITIES",
    "Community",
    "CommunityCode",
    "community",
    "DNI",
    "InvalidSpanishId",
    "MAX_LENGTH",
    "NIE",
    "NIF",
    "NIF_KINDS",
    "parse_spanish_id",
    "PERSON_KINDS",
    "PROVINCES",
    "Province",
    "ProvinceCode",
    "province",
    "province_for_postal_code",
    "SpanishId",
    "SpanishIdKind",
    "SpanishIdKinds",
    "SpanishPostalCode",
    "UnknownProvince",
]
