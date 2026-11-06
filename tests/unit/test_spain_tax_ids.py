"""Spanish tax IDs: each kind, each rule of the CIF's control character, the normalization, and
the Pydantic types."""

from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from fastapi_alcazaba.countries.spain import (
    CIF,
    DNI,
    MAX_LENGTH,
    NIE,
    NIF,
    PERSON_KINDS,
    InvalidSpanishId,
    SpanishId,
    SpanishIdKind,
    SpanishIdKinds,
    parse_spanish_id,
)

K = SpanishIdKind
PersonNIF = Annotated[str, SpanishIdKinds(*PERSON_KINDS)]


class Customer(BaseModel):
    nif: NIF


@pytest.mark.parametrize(
    ("value", "normalized", "kind"),
    [
        ("12345678Z", "12345678Z", K.DNI),
        ("12345678z", "12345678Z", K.DNI),
        ("12.345.678-Z", "12345678Z", K.DNI),
        (" 12345678 Z ", "12345678Z", K.DNI),
        ("1234567L", "01234567L", K.DNI),  # written without its leading zero
        ("00000002W", "00000002W", K.DNI),
        ("X1234567L", "X1234567L", K.NIE),
        ("x-123456-s", "X0123456S", K.NIE),
        ("Y1234567X", "Y1234567X", K.NIE),  # Y counts as 1: 11234567 % 23 is 10, X
        ("Z1234567R", "Z1234567R", K.NIE),  # Z counts as 2: 21234567 % 23 is 1, R
        ("K0123456S", "K0123456S", K.NIF_KLM),
        ("L0123456S", "L0123456S", K.NIF_KLM),
        ("m0123456s", "M0123456S", K.NIF_KLM),
        # Real ones: the tax agency (Q, a letter only) and Telefónica (A, a digit only).
        ("Q2826000H", "Q2826000H", K.CIF),
        ("A28015865", "A28015865", K.CIF),
        ("B12345674", "B12345674", K.CIF),
        ("b-1234567-4", "B12345674", K.CIF),
        # C, D, F, G, J, U, V take the letter or the digit: 1234567's are D and 4.
        ("G12345674", "G12345674", K.CIF),
        ("G1234567D", "G1234567D", K.CIF),
        ("J1234567D", "J1234567D", K.CIF),
        ("P1234567D", "P1234567D", K.CIF),  # P, Q, R, S, W, N: only the letter
        ("N1234567D", "N1234567D", K.CIF),
        # A non-resident entity (number 00...) takes the letter, whatever its kind: 0012345's
        # sum is 3+1+6+5 = 15, control 5, E.
        ("G0012345E", "G0012345E", K.CIF),
    ],
)
def test_valid_ids(value: str, normalized: str, kind: SpanishIdKind) -> None:
    parsed = parse_spanish_id(value)
    assert parsed == SpanishId(normalized, kind)
    assert str(parsed) == normalized
    assert Customer(nif=value).nif == normalized


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("12345678A", "the DNI's control letter doesn't match"),
        ("X1234567A", "the NIE's control letter doesn't match"),
        ("K0123456A", "the NIF_KLM's control letter doesn't match"),
        ("K123456S", "a NIF K, L or M has 7 digits"),
        ("00000000T", "a placeholder, not a DNI"),
        ("00000001R", "a placeholder, not a DNI"),
        ("99999999R", "a placeholder, not a DNI"),
        ("X0000000T", "a placeholder, not a NIE"),
        ("XT", "not a valid NIE"),
        ("X12345678", "not a Spanish tax ID: it must end with a letter"),
        ("X12345678Z", "not a Spanish tax ID"),  # 10 characters
        ("123456789", "not a Spanish tax ID: it must end with a letter"),
        ("1234A678Z", "not a valid DNI"),
        ("B12345675", "the CIF's control character doesn't match"),
        ("B1234567D", "the CIF's control character doesn't match"),  # B: only the digit
        ("A2801586F", "the CIF's control character doesn't match"),  # A: only the digit
        ("P12345674", "the CIF's control character doesn't match"),  # P: only the letter
        ("G00123455", "the CIF's control character doesn't match"),  # 00...: only the letter
        ("G1234567A", "the CIF's control character doesn't match"),
        ("B1234567", "a CIF is a letter, 7 digits and a control character"),
        ("B1234A674", "a CIF is a letter, 7 digits and a control character"),
        ("I12345674", "not a Spanish tax ID"),  # I, O, T aren't a kind of entity
        ("", "not a Spanish tax ID"),
        ("Z", "not a Spanish tax ID"),
        ("12345678/Z", "not a Spanish tax ID"),  # only spaces, hyphens and dots go
        ("12345678_Z", "not a Spanish tax ID"),
        ("١٢٣٤٥٦٧٨Z", "not a Spanish tax ID"),
        ("１２３４５６７８Z", "not a Spanish tax ID"),
        ("12345678K", "not a Spanish tax ID"),  # the Kelvin sign, a K once lowercased
        ("1" * (MAX_LENGTH + 1), "too long for a Spanish tax ID"),
        ("-" * (MAX_LENGTH + 1), "too long for a Spanish tax ID"),
    ],
)
def test_invalid_ids(value: str, message: str) -> None:
    with pytest.raises(InvalidSpanishId) as raised:
        parse_spanish_id(value)
    assert str(raised.value) == message
    assert isinstance(raised.value, ValueError)


def test_errors_never_hold_the_value() -> None:
    for value in ["12345678A", "X1234567A", "B12345675", "12345678@Z", "B12345674"]:
        with pytest.raises(InvalidSpanishId) as raised:
            parse_spanish_id(value, kinds={K.DNI})
        assert value not in str(raised.value)
        assert value[:8] not in str(raised.value)


def test_only_some_kinds() -> None:
    assert parse_spanish_id("12345678Z", kinds={K.DNI}).kind is K.DNI
    with pytest.raises(InvalidSpanishId, match="a CIF isn't accepted here, only: DNI, NIE"):
        parse_spanish_id("B12345674", kinds={K.NIE, K.DNI})


def test_a_non_string_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="not int"):
        parse_spanish_id(12345678)  # type: ignore[arg-type]


class Person(BaseModel):
    dni: DNI | None = None
    nie: NIE | None = None
    cif: CIF | None = None
    person: PersonNIF | None = None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dni", "X1234567L"),
        ("dni", "K0123456S"),  # a NIF K/L/M is no DNI
        ("nie", "12345678Z"),
        ("cif", "12345678Z"),
        ("person", "B12345674"),
        ("dni", 12345678),
        ("dni", "12345678A"),
    ],
)
def test_a_field_refuses_other_kinds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Person.model_validate({field: value})


def test_fields_give_the_normalized_value() -> None:
    person = Person(dni="1234567-l", nie="x1234567l", cif="b-12345674", person="K0123456S")
    assert person.model_dump() == {
        "dni": "01234567L",
        "nie": "X1234567L",
        "cif": "B12345674",
        "person": "K0123456S",
    }


def test_the_marker_needs_a_kind() -> None:
    with pytest.raises(ValueError, match="at least one kind"):
        SpanishIdKinds()
    assert repr(SpanishIdKinds(K.NIE, K.DNI)) == "SpanishIdKinds(DNI, NIE)"


def test_the_json_schema_and_a_422() -> None:
    app = FastAPI()

    @app.post("/customers")
    async def create(customer: Customer, person: Person) -> Customer:
        assert person.dni is None
        return customer

    client = TestClient(app)
    body = {"customer": {"nif": "b-12345674"}, "person": {}}
    assert client.post("/customers", json=body).json() == {"nif": "B12345674"}
    body = {"customer": {"nif": "12345678A"}, "person": {}}
    response = client.post("/customers", json=body)
    assert response.status_code == 422
    assert "the DNI's control letter doesn't match" in response.text
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["Customer"]["properties"]["nif"] == {
        "type": "string",
        "maxLength": MAX_LENGTH,
        "examples": ["12345678Z"],
        "title": "Nif",
    }
    assert schemas["Person"]["properties"]["cif"]["anyOf"][0]["examples"] == ["B12345674"]
