"""The countries example: customers from anywhere, Spanish ones with their NIF and province
checked."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.countries.main import app, customers

ADDRESS = {"street": "Calle Recogidas 1", "city": "Granada", "postal_code": "18001"}


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    customers.clear()
    with TestClient(app) as client:
        yield client


def customer(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Aceites del Sur SL",
        "tax_id": "b-12345674",
        "address": {**ADDRESS, "country": "es"},
    }
    body.update(changes)
    return body


def test_the_countries_for_a_form(client: TestClient) -> None:
    countries = client.get("/countries").json()
    assert len(countries) == 249
    assert {"code": "ES", "name": "Spain", "flag": "\U0001f1ea\U0001f1f8"} in countries


@pytest.mark.parametrize("code", ["ES", "esp", "724"])
def test_a_country_by_any_code(client: TestClient, code: str) -> None:
    assert client.get(f"/countries/{code}").json()["name"] == "Spain"


def test_an_unknown_country_is_a_404(client: TestClient) -> None:
    response = client.get("/countries/XK")
    assert response.status_code == 404
    assert response.json() == {"detail": "No such country"}


def test_the_provinces(client: TestClient) -> None:
    assert len(client.get("/spain/provinces").json()) == 52
    andalucia = client.get("/spain/provinces", params={"community": "ES-AN"}).json()
    assert len(andalucia) == 8
    assert andalucia[0] == {
        "code": 4,
        "iso_code": "ES-AL",
        "name": "Almería",
        "community": "Andalucía",
    }
    assert client.get("/spain/provinces", params={"community": "13"}).json()[0]["name"] == (
        "Madrid"
    )
    assert client.get("/spain/provinces", params={"community": "99"}).status_code == 422


def test_a_spanish_customer_gets_their_nif_normalized_and_their_province(
    client: TestClient,
) -> None:
    response = client.post("/customers", json=customer())
    assert response.status_code == 201
    created = response.json()
    assert created["tax_id"] == "B12345674"
    assert created["tax_id_kind"] == "CIF"
    assert created["address"]["country"] == "ES"
    assert created["province"]["name"] == "Granada"
    assert created["province"]["community"] == "Andalucía"
    assert client.get(f"/customers/{created['id']}").json() == created


def test_a_person(client: TestClient) -> None:
    created = client.post("/customers", json=customer(tax_id="x-1234567-l")).json()
    assert (created["tax_id"], created["tax_id_kind"]) == ("X1234567L", "NIE")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tax_id": "B12345675"}, "the CIF's control character doesn't match"),
        ({"tax_id": "12345678A"}, "the DNI's control letter doesn't match"),
        ({"address": {**ADDRESS, "country": "ES", "postal_code": "99001"}}, "postal code"),
        ({"address": {**ADDRESS, "country": "XX"}}, "not an ISO 3166-1 alpha-2 country code"),
        ({"address": {**ADDRESS, "country": "ESP"}}, "not an ISO 3166-1 alpha-2 country code"),
    ],
)
def test_wrong_ones_are_a_422(client: TestClient, changes: dict[str, Any], message: str) -> None:
    response = client.post("/customers", json=customer(**changes))
    assert response.status_code == 422
    assert message in response.text
    assert customers == {}


def test_others_keep_their_tax_id_as_written(client: TestClient) -> None:
    address = {"street": "1 rue de Rivoli", "city": "Paris", "postal_code": "75001"}
    address["country"] = "FR"
    created = client.post("/customers", json=customer(tax_id="FR40303265045", address=address))
    assert created.status_code == 201
    assert created.json()["tax_id"] == "FR40303265045"
    assert created.json()["tax_id_kind"] is None
    assert created.json()["province"] is None


def test_the_client_cant_set_the_province(client: TestClient) -> None:
    province = {"code": 28, "iso_code": "ES-M", "name": "Madrid", "community": "Madrid"}
    body = customer(address={**ADDRESS, "country": "FR"}, province=province, tax_id_kind="DNI")
    created = client.post("/customers", json=body).json()
    assert (created["province"], created["tax_id_kind"]) == (None, None)
    body = customer(_province=province, _tax_id_kind="DNI")
    created = client.post("/customers", json=body).json()
    assert (created["province"]["name"], created["tax_id_kind"]) == ("Granada", "CIF")


def test_an_unknown_customer(client: TestClient) -> None:
    assert client.get("/customers/1").status_code == 404
