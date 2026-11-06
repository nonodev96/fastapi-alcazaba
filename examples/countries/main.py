"""An invoicing API's customers, from anywhere: Spanish ones with their NIF and province checked.

    uvicorn examples.countries.main:app

- `GET /countries`: every country, for a form's list: its code, name and flag.
- `GET /countries/{code}`: one, by any of its codes (`ES`, `ESP`, `724`).
- `GET /spain/provinces`: Spain's provinces, all or those of an autonomous community.
- `POST /customers`: a customer, whose address's country is an ISO 3166-1 code. A Spanish one
  (`ES`) must have a valid NIF (a DNI, NIE, NIF K/L/M or a company's CIF) and a postal code of
  a province, which the customer is given; others keep their tax ID as written.
- `GET /customers/{customer_id}`: one back.

A wrong code, NIF or postal code is a 422 that says what's wrong, never a 500.
"""

from typing import Annotated, Self

import uvicorn
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from fastapi_alcazaba.countries import COUNTRIES, CountryCode, UnknownCountry, country
from fastapi_alcazaba.countries.spain import (
    MAX_LENGTH,
    PROVINCES,
    Province,
    SpanishIdKind,
    UnknownProvince,
    community,
    parse_spanish_id,
    province_for_postal_code,
)

app = FastAPI(title="Customers")


class CountryOut(BaseModel):
    code: str
    name: str
    flag: str


class ProvinceOut(BaseModel):
    code: int  # the INE's: the first two digits of its postal codes
    iso_code: str
    name: str
    community: str


class Address(BaseModel):
    street: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=12)
    country: CountryCode


class NewCustomer(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str = Field(min_length=1, max_length=MAX_LENGTH)
    address: Address
    # Set for Spanish customers, from their NIF and postal code. Private attributes: a client
    # can't send them (a field with exclude=True would still be read from the body).
    _tax_id_kind: SpanishIdKind | None = PrivateAttr(default=None)
    _province: ProvinceOut | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def spanish_rules(self) -> Self:
        """In Spain, the NIF and the postal code are checked: InvalidSpanishId and
        UnknownProvince are ValueErrors, which Pydantic turns into a 422."""
        if self.address.country == "ES":
            tax_id = parse_spanish_id(self.tax_id)
            found = province_for_postal_code(self.address.postal_code)
            self.tax_id, self._tax_id_kind = tax_id.value, tax_id.kind
            self._province = _province_out(found)
        return self


class Customer(BaseModel):
    id: int
    name: str
    tax_id: str
    tax_id_kind: SpanishIdKind | None
    address: Address
    province: ProvinceOut | None


customers: dict[int, Customer] = {}


def _province_out(found: Province) -> ProvinceOut:
    return ProvinceOut(
        code=found.code,
        iso_code=found.iso_code,
        name=found.name,
        community=found.community.name,
    )


@app.get("/countries")
async def list_countries() -> list[CountryOut]:
    return [CountryOut(code=c.alpha_2, name=c.name, flag=c.flag) for c in COUNTRIES.values()]


@app.get("/countries/{code}")
async def read_country(code: str) -> CountryOut:
    try:
        found = country(code)
    except UnknownCountry:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such country") from None
    return CountryOut(code=found.alpha_2, name=found.name, flag=found.flag)


@app.get("/spain/provinces")
async def list_provinces(
    community_code: Annotated[str | None, Query(alias="community", max_length=8)] = None,
) -> list[ProvinceOut]:
    """`?community=13` or `?community=ES-MD`: those of one autonomous community."""
    try:
        chosen = PROVINCES if community_code is None else community(community_code).provinces
    except UnknownProvince:
        raise HTTPException(422, "No such community") from None
    return [_province_out(found) for found in chosen]


@app.post("/customers", status_code=status.HTTP_201_CREATED)
async def create_customer(new: NewCustomer) -> Customer:
    customer = Customer(
        id=len(customers) + 1,
        name=new.name,
        tax_id=new.tax_id,
        tax_id_kind=new._tax_id_kind,  # pylint: disable=protected-access
        address=new.address,
        province=new._province,  # pylint: disable=protected-access
    )
    customers[customer.id] = customer
    return customer


@app.get("/customers/{customer_id}")
async def read_customer(customer_id: int) -> Customer:
    if customer_id not in customers:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such customer")
    return customers[customer_id]


if __name__ == "__main__":
    uvicorn.run(app)
