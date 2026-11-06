"""ISO 3166-1's countries: the table, the lookup by any code, and the Pydantic type."""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from fastapi_alcazaba.countries import COUNTRIES, Country, CountryCode, UnknownCountry, country

ISO_CODES = Path("/usr/share/iso-codes/json/iso_3166-1.json")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_countries.py"


class Address(BaseModel):
    country: CountryCode


def test_the_table_holds_the_249_assigned_codes_by_alpha_2() -> None:
    assert len(COUNTRIES) == 249
    assert list(COUNTRIES) == sorted(COUNTRIES)
    assert len({c.alpha_3 for c in COUNTRIES.values()}) == 249
    assert len({c.numeric for c in COUNTRIES.values()}) == 249
    for code, found in COUNTRIES.items():
        assert found.alpha_2 == code
        assert len(code) == 2
        assert code.isupper()
        assert len(found.alpha_3) == 3
        assert found.alpha_3.isupper()
        assert len(found.numeric) == 3
        assert found.numeric.isdigit()
        assert found.name


def test_the_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        COUNTRIES["XK"] = Country("XK", "XKX", "999", "Kosovo")  # type: ignore[index]


def test_spain() -> None:
    spain = COUNTRIES["ES"]
    assert spain == Country("ES", "ESP", "724", "Spain", "Kingdom of Spain")
    assert spain.flag == "\U0001f1ea\U0001f1f8"
    assert COUNTRIES["AE"].official_name is None


@pytest.mark.parametrize("code", ["ES", "es", " Es ", "ESP", "esp", "724", 724, " 724 "])
def test_country_by_any_code(code: str | int) -> None:
    assert country(code) is COUNTRIES["ES"]


def test_numeric_codes_keep_their_zeros() -> None:
    afghanistan = country("AF")
    assert afghanistan.numeric == "004"
    assert country(4) is country("4") is country("004") is afghanistan


@pytest.mark.parametrize(
    "code",
    [
        "XX",
        "XK",  # user-assigned (Kosovo), not ISO's
        "EU",  # reserved
        "UK",  # reserved: the United Kingdom is GB
        "",
        "E",
        "ESPA",
        "999",
        "0000",
        -1,
        1000,
        True,  # a bool isn't the number 1
        7.0,
        None,
        "ß",  # "SS" once uppercased: South Sudan
        "ＥＳ",  # fullwidth ES
        "٧٢٤",  # Arabic-Indic 724
        "ES" + " " * 10,
        "E" * 1_000_000,
    ],
)
def test_unknown_codes(code: object) -> None:
    with pytest.raises(UnknownCountry) as raised:
        country(code)  # type: ignore[arg-type]
    assert str(raised.value) == "not an ISO 3166-1 country code"


@pytest.mark.parametrize("value", ["es", "ES", " es "])
def test_the_field_gives_uppercase(value: str) -> None:
    assert Address(country=value).country == "ES"


@pytest.mark.parametrize("value", ["XX", "ESP", "", "ß", "ＥＳ", "e" * 1_000_000, 34])
def test_the_field_refuses_others(value: object) -> None:
    with pytest.raises(ValidationError):
        Address(country=value)  # type: ignore[arg-type]


def test_a_request_with_a_wrong_code_gets_a_422() -> None:
    app = FastAPI()

    @app.post("/addresses")
    async def create(address: Address) -> Address:
        return address

    client = TestClient(app)
    assert client.post("/addresses", json={"country": "es"}).json() == {"country": "ES"}
    response = client.post("/addresses", json={"country": "XX"})
    assert response.status_code == 422
    assert "not an ISO 3166-1 alpha-2 country code" in response.text
    schema = app.openapi()["components"]["schemas"]["Address"]["properties"]["country"]
    assert schema["pattern"] == "^[A-Za-z]{2}$"


@pytest.mark.skipif(not ISO_CODES.exists(), reason="needs Debian's iso-codes")
def test_the_table_and_spain_match_iso_codes() -> None:
    result = subprocess.run(  # noqa: S603  # our own interpreter and script
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
