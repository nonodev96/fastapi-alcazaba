"""Spain's provinces and autonomous communities, by INE and ISO code, and its postal codes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from fastapi_alcazaba.countries.spain import (
    COMMUNITIES,
    PROVINCES,
    CommunityCode,
    ProvinceCode,
    SpanishPostalCode,
    UnknownProvince,
    community,
    province,
    province_for_postal_code,
)


class Address(BaseModel):
    postal_code: SpanishPostalCode
    province: ProvinceCode


def test_the_52_provinces_and_19_communities_by_ine_code() -> None:
    assert [p.code for p in PROVINCES] == list(ProvinceCode) == list(range(1, 53))
    assert [c.code for c in COMMUNITIES] == list(CommunityCode) == list(range(1, 20))
    assert len({p.iso_code for p in PROVINCES}) == 52
    assert len({c.iso_code for c in COMMUNITIES}) == 19
    for each in PROVINCES:
        assert each.iso_code.startswith("ES-")
        assert each.name
    for one in COMMUNITIES:
        assert one.iso_code.startswith("ES-")
        assert one.name


def test_every_community_has_its_provinces() -> None:
    assert sum(len(c.provinces) for c in COMMUNITIES) == 52
    assert [p.name for p in community(CommunityCode.CANARIAS).provinces] == [
        "Las Palmas",
        "Santa Cruz de Tenerife",
    ]
    assert len(community("ES-AN").provinces) == 8
    assert len(community("es-cl").provinces) == 9


@pytest.mark.parametrize("code", [ProvinceCode.MADRID, 28, "28", " 28 ", "ES-M", "es-m"])
def test_a_province_by_any_code(code: ProvinceCode | int | str) -> None:
    madrid = province(code)
    assert (madrid.code, madrid.name, madrid.iso_code) == (28, "Madrid", "ES-M")
    assert madrid.community is community(CommunityCode.MADRID)
    assert madrid.community.name == "Comunidad de Madrid"


def test_leading_zeros_and_the_users_names() -> None:
    assert province("07") is province(ProvinceCode.BALEARS)
    assert province("7").name == "Illes Balears"
    assert province(ProvinceCode.CORUNIA).iso_code == "ES-C"
    assert province(ProvinceCode.ARABA).name == "Araba/Álava"


def test_ceuta_and_melilla_are_a_province_and_a_city() -> None:
    ceuta = province(ProvinceCode.CEUTA)
    assert ceuta.iso_code == ceuta.community.iso_code == "ES-CE"
    assert province("ES-ML").community is community(19)


@pytest.mark.parametrize(
    "code",
    [0, 53, -1, "53", "028", "ES-XX", "ES-AN", "", "M", True, 2.0, None, "٢٨"],
)
def test_unknown_provinces(code: object) -> None:
    with pytest.raises(UnknownProvince, match="not the code of a Spanish province"):
        province(code)  # type: ignore[arg-type]


@pytest.mark.parametrize("code", [0, 20, "20", "ES-M", "x" * 100])
def test_unknown_communities(code: object) -> None:
    with pytest.raises(UnknownProvince, match="autonomous community"):
        community(code)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("postal_code", "name"),
    [("18001", "Granada"), ("01001", "Araba/Álava"), (" 28013 ", "Madrid"), ("52001", "Melilla")],
)
def test_the_province_of_a_postal_code(postal_code: str, name: str) -> None:
    assert province_for_postal_code(postal_code).name == name
    address = Address(postal_code=postal_code, province=ProvinceCode.ARABA)
    assert address.postal_code == postal_code.strip()


@pytest.mark.parametrize(
    "postal_code",
    ["53001", "00000", "00123", "1800", "180010", "18 001", "١٨٠٠١", ""],
)
def test_wrong_postal_codes(postal_code: str) -> None:
    with pytest.raises(UnknownProvince, match="not a Spanish postal code"):
        province_for_postal_code(postal_code)
    with pytest.raises(ValidationError):
        Address(postal_code=postal_code, province=ProvinceCode.ARABA)


def test_the_province_field_in_a_request() -> None:
    app = FastAPI()

    @app.get("/provinces/{code}")
    async def read(code: ProvinceCode) -> dict[str, str]:
        return {"name": province(code).name}

    @app.post("/addresses")
    async def create(address: Address) -> Address:
        return address

    client = TestClient(app)
    assert client.get("/provinces/18").json() == {"name": "Granada"}
    assert client.get("/provinces/53").status_code == 422
    body = {"postal_code": "18001", "province": 18}
    assert client.post("/addresses", json=body).json() == body
    assert client.post("/addresses", json={**body, "postal_code": "99001"}).status_code == 422
