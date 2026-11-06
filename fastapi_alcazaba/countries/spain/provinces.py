"""Spain's 52 provinces (Ceuta and Melilla among them) and 19 autonomous communities and cities,
by their INE codes, with their ISO 3166-2 codes; and the province of a postal code, whose first
two digits are its INE code."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType
from typing import Annotated

from pydantic import AfterValidator, WithJsonSchema


class UnknownProvince(LookupError, ValueError):
    """No province, or no community, has that code. A `ValueError` too: raised in a Pydantic
    validator, it's a 422."""


class CommunityCode(IntEnum):
    """An autonomous community's or city's INE code."""

    ANDALUCIA = 1
    ARAGON = 2
    ASTURIAS = 3
    BALEARS = 4
    CANARIAS = 5
    CANTABRIA = 6
    CASTILLA_LEON = 7
    CASTILLA_LA_MANCHA = 8
    CATALUNIA = 9
    VALENCIANA = 10
    EXTREMADURA = 11
    GALICIA = 12
    MADRID = 13
    MURCIA = 14
    NAVARRA = 15
    PAIS_VASCO = 16
    RIOJA = 17
    CEUTA = 18
    MELILLA = 19


class ProvinceCode(IntEnum):
    """A province's INE code: the first two digits of its postal codes."""

    ARABA = 1
    ALBACETE = 2
    ALICANTE = 3
    ALMERIA = 4
    AVILA = 5
    BADAJOZ = 6
    BALEARS = 7
    BARCELONA = 8
    BURGOS = 9
    CACERES = 10
    CADIZ = 11
    CASTELLON = 12
    CIUDAD_REAL = 13
    CORDOBA = 14
    CORUNIA = 15
    CUENCA = 16
    GIRONA = 17
    GRANADA = 18
    GUADALAJARA = 19
    GIPUZKOA = 20
    HUELVA = 21
    HUESCA = 22
    JAEN = 23
    LEON = 24
    LLEIDA = 25
    RIOJA = 26
    LUGO = 27
    MADRID = 28
    MALAGA = 29
    MURCIA = 30
    NAVARRA = 31
    OURENSE = 32
    ASTURIAS = 33
    PALENCIA = 34
    PALMAS = 35
    PONTEVEDRA = 36
    SALAMANCA = 37
    TENERIFE = 38
    CANTABRIA = 39
    SEGOVIA = 40
    SEVILLA = 41
    SORIA = 42
    TARRAGONA = 43
    TERUEL = 44
    TOLEDO = 45
    VALENCIA = 46
    VALLADOLID = 47
    BIZKAIA = 48
    ZAMORA = 49
    ZARAGOZA = 50
    CEUTA = 51
    MELILLA = 52


@dataclass(frozen=True, slots=True)
class Community:
    """An autonomous community or city."""

    code: CommunityCode
    #: Its name as the INE writes it, in natural order: `Comunidad de Madrid`.
    name: str
    #: ISO 3166-2: `ES-MD`.
    iso_code: str

    @property
    def provinces(self) -> tuple["Province", ...]:
        """Its provinces, by INE code."""
        return tuple(province for province in PROVINCES if province.community is self)


@dataclass(frozen=True, slots=True)
class Province:
    """A province. Ceuta and Melilla are a province and an autonomous city at once."""

    code: ProvinceCode
    #: Its name as the INE writes it, in natural order: `A Coruña`, `Alicante/Alacant`.
    name: str
    #: ISO 3166-2: `ES-M` (Ceuta's and Melilla's are their cities', `ES-CE` and `ES-ML`).
    iso_code: str
    community: Community


_C = CommunityCode
COMMUNITIES: tuple[Community, ...] = tuple(
    Community(code, name, f"ES-{iso}")
    for code, name, iso in (
        (_C.ANDALUCIA, "Andalucía", "AN"),
        (_C.ARAGON, "Aragón", "AR"),
        (_C.ASTURIAS, "Principado de Asturias", "AS"),
        (_C.BALEARS, "Illes Balears", "IB"),
        (_C.CANARIAS, "Canarias", "CN"),
        (_C.CANTABRIA, "Cantabria", "CB"),
        (_C.CASTILLA_LEON, "Castilla y León", "CL"),
        (_C.CASTILLA_LA_MANCHA, "Castilla-La Mancha", "CM"),
        (_C.CATALUNIA, "Cataluña/Catalunya", "CT"),
        (_C.VALENCIANA, "Comunitat Valenciana", "VC"),
        (_C.EXTREMADURA, "Extremadura", "EX"),
        (_C.GALICIA, "Galicia", "GA"),
        (_C.MADRID, "Comunidad de Madrid", "MD"),
        (_C.MURCIA, "Región de Murcia", "MC"),
        (_C.NAVARRA, "Comunidad Foral de Navarra", "NC"),
        (_C.PAIS_VASCO, "País Vasco/Euskadi", "PV"),
        (_C.RIOJA, "La Rioja", "RI"),
        (_C.CEUTA, "Ceuta", "CE"),
        (_C.MELILLA, "Melilla", "ML"),
    )
)

_P = ProvinceCode
PROVINCES: tuple[Province, ...] = tuple(
    Province(code, name, f"ES-{iso}", COMMUNITIES[community - 1])
    for code, name, iso, community in (
        (_P.ARABA, "Araba/Álava", "VI", _C.PAIS_VASCO),
        (_P.ALBACETE, "Albacete", "AB", _C.CASTILLA_LA_MANCHA),
        (_P.ALICANTE, "Alicante/Alacant", "A", _C.VALENCIANA),
        (_P.ALMERIA, "Almería", "AL", _C.ANDALUCIA),
        (_P.AVILA, "Ávila", "AV", _C.CASTILLA_LEON),
        (_P.BADAJOZ, "Badajoz", "BA", _C.EXTREMADURA),
        (_P.BALEARS, "Illes Balears", "PM", _C.BALEARS),
        (_P.BARCELONA, "Barcelona", "B", _C.CATALUNIA),
        (_P.BURGOS, "Burgos", "BU", _C.CASTILLA_LEON),
        (_P.CACERES, "Cáceres", "CC", _C.EXTREMADURA),
        (_P.CADIZ, "Cádiz", "CA", _C.ANDALUCIA),
        (_P.CASTELLON, "Castellón/Castelló", "CS", _C.VALENCIANA),
        (_P.CIUDAD_REAL, "Ciudad Real", "CR", _C.CASTILLA_LA_MANCHA),
        (_P.CORDOBA, "Córdoba", "CO", _C.ANDALUCIA),
        (_P.CORUNIA, "A Coruña", "C", _C.GALICIA),
        (_P.CUENCA, "Cuenca", "CU", _C.CASTILLA_LA_MANCHA),
        (_P.GIRONA, "Girona", "GI", _C.CATALUNIA),
        (_P.GRANADA, "Granada", "GR", _C.ANDALUCIA),
        (_P.GUADALAJARA, "Guadalajara", "GU", _C.CASTILLA_LA_MANCHA),
        (_P.GIPUZKOA, "Gipuzkoa", "SS", _C.PAIS_VASCO),
        (_P.HUELVA, "Huelva", "H", _C.ANDALUCIA),
        (_P.HUESCA, "Huesca", "HU", _C.ARAGON),
        (_P.JAEN, "Jaén", "J", _C.ANDALUCIA),
        (_P.LEON, "León", "LE", _C.CASTILLA_LEON),
        (_P.LLEIDA, "Lleida", "L", _C.CATALUNIA),
        (_P.RIOJA, "La Rioja", "LO", _C.RIOJA),
        (_P.LUGO, "Lugo", "LU", _C.GALICIA),
        (_P.MADRID, "Madrid", "M", _C.MADRID),
        (_P.MALAGA, "Málaga", "MA", _C.ANDALUCIA),
        (_P.MURCIA, "Murcia", "MU", _C.MURCIA),
        (_P.NAVARRA, "Navarra", "NA", _C.NAVARRA),
        (_P.OURENSE, "Ourense", "OR", _C.GALICIA),
        (_P.ASTURIAS, "Asturias", "O", _C.ASTURIAS),
        (_P.PALENCIA, "Palencia", "P", _C.CASTILLA_LEON),
        (_P.PALMAS, "Las Palmas", "GC", _C.CANARIAS),
        (_P.PONTEVEDRA, "Pontevedra", "PO", _C.GALICIA),
        (_P.SALAMANCA, "Salamanca", "SA", _C.CASTILLA_LEON),
        (_P.TENERIFE, "Santa Cruz de Tenerife", "TF", _C.CANARIAS),
        (_P.CANTABRIA, "Cantabria", "S", _C.CANTABRIA),
        (_P.SEGOVIA, "Segovia", "SG", _C.CASTILLA_LEON),
        (_P.SEVILLA, "Sevilla", "SE", _C.ANDALUCIA),
        (_P.SORIA, "Soria", "SO", _C.CASTILLA_LEON),
        (_P.TARRAGONA, "Tarragona", "T", _C.CATALUNIA),
        (_P.TERUEL, "Teruel", "TE", _C.ARAGON),
        (_P.TOLEDO, "Toledo", "TO", _C.CASTILLA_LA_MANCHA),
        (_P.VALENCIA, "Valencia/València", "V", _C.VALENCIANA),
        (_P.VALLADOLID, "Valladolid", "VA", _C.CASTILLA_LEON),
        (_P.BIZKAIA, "Bizkaia", "BI", _C.PAIS_VASCO),
        (_P.ZAMORA, "Zamora", "ZA", _C.CASTILLA_LEON),
        (_P.ZARAGOZA, "Zaragoza", "Z", _C.ARAGON),
        (_P.CEUTA, "Ceuta", "CE", _C.CEUTA),
        (_P.MELILLA, "Melilla", "ML", _C.MELILLA),
    )
)

_PROVINCES_BY_ISO: Mapping[str, Province] = MappingProxyType(
    {province.iso_code: province for province in PROVINCES},
)
_COMMUNITIES_BY_ISO: Mapping[str, Community] = MappingProxyType(
    {community.iso_code: community for community in COMMUNITIES},
)


def _code(value: object, size: int) -> int | str | None:
    """An INE code as an int (`28`, `"28"`, `"07"`), an ISO code in uppercase, or None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str) or len(value) > 8 or not value.isascii():
        return None
    text = value.strip(" ").upper()
    if text.isdigit():
        return int(text) if len(text) <= size else None
    return text


def province(value: ProvinceCode | int | str) -> Province:
    """The province of an INE code (`ProvinceCode.MADRID`, `28`, `"28"`) or an ISO 3166-2 code
    (`ES-M`, in any case).

    :raises UnknownProvince: if no province has it.
    """
    code = _code(value, 2)
    if isinstance(code, int) and 1 <= code <= len(PROVINCES):
        return PROVINCES[code - 1]
    if isinstance(code, str) and code in _PROVINCES_BY_ISO:
        return _PROVINCES_BY_ISO[code]
    raise UnknownProvince("not the code of a Spanish province")


def community(value: CommunityCode | int | str) -> Community:
    """The autonomous community or city of an INE code (`CommunityCode.MADRID`, `13`, `"13"`) or
    an ISO 3166-2 code (`ES-MD`, in any case).

    :raises UnknownProvince: if no community has it.
    """
    code = _code(value, 2)
    if isinstance(code, int) and 1 <= code <= len(COMMUNITIES):
        return COMMUNITIES[code - 1]
    if isinstance(code, str) and code in _COMMUNITIES_BY_ISO:
        return _COMMUNITIES_BY_ISO[code]
    raise UnknownProvince("not the code of a Spanish autonomous community")


def _postal_code(value: str) -> str:
    code = value.strip(" ") if len(value) <= 8 else ""
    if not (len(code) == 5 and code.isascii() and code.isdigit() and 1 <= int(code[:2]) <= 52):
        raise ValueError("not a Spanish postal code")
    return code


def province_for_postal_code(postal_code: str) -> Province:
    """The province of a Spanish postal code: `18001` is in Granada.

    :raises UnknownProvince: if it isn't five digits whose first two are a province's.
    """
    try:
        code = _postal_code(postal_code)
    except ValueError:
        raise UnknownProvince("not a Spanish postal code") from None
    return PROVINCES[int(code[:2]) - 1]


#: A field that takes a Spanish postal code (five digits, of a province: `18001`).
SpanishPostalCode = Annotated[
    str,
    AfterValidator(_postal_code),
    WithJsonSchema({"type": "string", "pattern": "^[0-9]{5}$", "examples": ["28013"]}),
]
