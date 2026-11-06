# Countries and Spain

In an app: [the example](../examples/countries.md). Every class and function: [the API reference](../reference/countries.md).

Addresses, invoices and phone numbers need a country, and for Spain a tax ID and a province. The
codes are ISO's, the IDs are checked by their control character, and each has a Pydantic type
that normalizes what the client sends and refuses the rest with a 422. The tables are in the
package: nothing to set up, no backend, no extra to install.

```python
from fastapi_alcazaba.countries import CountryCode
from fastapi_alcazaba.countries.spain import NIF, SpanishPostalCode


class Customer(BaseModel):
    nif: NIF  # "12.345.678-z" → "12345678Z"; "12345678A" → 422
    postal_code: SpanishPostalCode  # "18001"; "53001" → 422
    country: CountryCode  # "es" → "ES"; "XX", "ESP" → 422
```

## Countries

`COUNTRIES` holds the 249 countries and territories that ISO 3166-1 assigns a code, by their
alpha-2 code. Each `Country` has the three codes, ISO's English name, its official name when it
has one, and its flag:

```pycon
>>> from fastapi_alcazaba.countries import COUNTRIES, country
>>> COUNTRIES["ES"]
Country(alpha_2='ES', alpha_3='ESP', numeric='724', name='Spain', official_name='Kingdom of Spain')
>>> country("esp") is country(724) is country(" es ")
True
>>> country(4).name, country(4).numeric, country("ES").flag
('Afghanistan', '004', '🇪🇸')
```

| Code | Example | Used by |
| --- | --- | --- |
| `alpha_2` | `ES` | language tags ([RFC 5646](https://www.rfc-editor.org/rfc/rfc5646): `es-ES`), country domains ([RFC 1591](https://www.rfc-editor.org/rfc/rfc1591)), most APIs |
| `alpha_3` | `ESP` | passports, the UN, sports |
| `numeric` | `724` | statistics; the same in any alphabet |

`country()` takes any of them, in any case, and raises `UnknownCountry` otherwise. Codes that
ISO reserves or that users assign aren't countries: `XK` (Kosovo), `EU`, `UK` (the United
Kingdom is `GB`).

**`CountryCode`** is a field that takes an alpha-2 code in any case, with spaces around, and
gives it in uppercase; anything else is a 422. Its JSON Schema says it's two letters, without
listing the 249: an enum that large would weigh on every schema that uses it.

!!! tip "Names in other languages"

    The names are ISO's, in English. Translate them in the app (Babel's
    `Locale("es").territories["ES"]` is "España"), keyed by the alpha-2 code.

## Spanish tax IDs

Every person and company in Spain has a NIF, of one of four kinds:

| `SpanishIdKind` | Whose | Form | Example |
| --- | --- | --- | --- |
| `DNI` | a Spanish citizen's | 8 digits and a letter | `12345678Z` |
| `NIE` | a foreigner's | X, Y or Z, 7 digits and a letter | `X1234567L` |
| `NIF_KLM` | a person with neither: K (Spanish under 14), L (Spanish living abroad), M (foreigner without NIE) | the letter, 7 digits and a letter | `K0123456S` |
| `CIF` | a company's or any legal entity's (called CIF before 2008) | a letter, 7 digits, and a digit or a letter | `B12345674` |

`parse_spanish_id()` normalizes the value (spaces, hyphens and dots removed, in uppercase, a
DNI's or NIE's number padded with zeros), checks its control character, and gives it with its
kind:

```pycon
>>> from fastapi_alcazaba.countries.spain import SpanishIdKind, parse_spanish_id
>>> parse_spanish_id("12.345.678-z")
SpanishId(value='12345678Z', kind=<SpanishIdKind.DNI: 'DNI'>)
>>> parse_spanish_id("1234567L").value  # written without its leading zero
'01234567L'
>>> parse_spanish_id("B12345674", kinds={SpanishIdKind.DNI})
Traceback (most recent call last):
  ...
fastapi_alcazaba.countries.spain.tax_ids.InvalidSpanishId: a CIF isn't accepted here, only: DNI
```

The checks:

- **A person's letter** is the number's remainder by 23, in `TRWAGMYFPDXBNJZSQVHLCKE`. A NIE's X,
  Y and Z count as 0, 1 and 2; a NIF K/L/M's letter is that of its seven digits.
- **A company's control character** is a Luhn sum of its seven digits. The first letter says
  which form it takes: a letter for public bodies, churches and non-residents (N, P, Q, R, S,
  W, and any number starting with 00), a digit for companies (A, B, E, H), either for the rest.
- **Placeholders** that pass the check but are no one's are refused: `00000000T`, `00000001R`,
  `99999999R`, the NIE `X0000000T`.

The fields give the normalized value, ready to store:

| Field | Takes |
| --- | --- |
| `DNI` | only a DNI |
| `NIE` | only a NIE |
| `CIF` | only a legal entity's NIF |
| `NIF` | any of the four |
| `Annotated[str, SpanishIdKinds(...)]` | the kinds you list |

```python
from typing import Annotated

from fastapi_alcazaba.countries.spain import PERSON_KINDS, SpanishIdKinds

PersonNIF = Annotated[str, SpanishIdKinds(*PERSON_KINDS)]  # a DNI, NIE or NIF K/L/M
```

The kind isn't kept in the field's value: when the app needs it (a company is invoiced with
VAT, a person maybe not), `parse_spanish_id(value).kind` gives it.

!!! warning "Valid isn't registered"

    The check proves the ID was written right, not that it belongs to anyone, nor to the one
    who sent it. Whether a company's NIF is registered for VAT inside the EU, VIES says; who
    owns a DNI, only an identity check does.

## Provinces and autonomous communities

`ProvinceCode` holds the 52 provinces by their INE code (Ceuta and Melilla among them, as the
INE counts them), and `CommunityCode` the 17 autonomous communities and 2 autonomous cities.
`PROVINCES` and `COMMUNITIES` have each one's name (the INE's, in natural order: "A Coruña",
"Illes Balears", "Alicante/Alacant"), its ISO 3166-2 code, and a province's community:

```pycon
>>> from fastapi_alcazaba.countries.spain import ProvinceCode, community, province
>>> granada = province(ProvinceCode.GRANADA)  # or 18, "18", "ES-GR"
>>> granada.name, granada.iso_code, granada.community.name
('Granada', 'ES-GR', 'Andalucía')
>>> [p.name for p in community("ES-CN").provinces]
['Las Palmas', 'Santa Cruz de Tenerife']
```

`province()` and `community()` take an INE code (an `int`, a string with or without its zero)
or an ISO code, and raise `UnknownProvince` otherwise. As a field, `ProvinceCode` takes the
number and lists the 52 in the OpenAPI schema.

### Postal codes

A Spanish postal code's first two digits are its province's INE code:

```pycon
>>> from fastapi_alcazaba.countries.spain import province_for_postal_code
>>> province_for_postal_code("18001").name
'Granada'
```

**`SpanishPostalCode`** is a field that takes five digits whose first two are a province's.

## Only in Spain

A form for customers from anywhere checks the Spanish rules only for Spanish addresses. The
lookups' errors are `ValueError`s, so in a model validator they become a 422:

```python
class NewCustomer(BaseModel):
    tax_id: str = Field(max_length=MAX_LENGTH)
    postal_code: str = Field(max_length=12)
    country: CountryCode

    @model_validator(mode="after")
    def spanish_rules(self) -> Self:
        if self.country == "ES":
            self.tax_id = parse_spanish_id(self.tax_id).value
            province_for_postal_code(self.postal_code)
        return self
```

The [example](../examples/countries.md) also gives each Spanish customer their province.

## What it protects against

- **Lookalike characters:** `\d` and `str.isdigit()` take other scripts' digits (`١٢٣`), and
  `"ß".upper()` is `"SS"`, South Sudan's code. Only ASCII is taken, and checked before it's
  uppercased; separators other than spaces, hyphens and dots are refused, not removed.
- **Large inputs:** a tax ID longer than `MAX_LENGTH` (20) characters and a code longer than 8
  are refused before any work.
- **Personal data in logs:** the errors' messages never hold the value. FastAPI's 422 shows the
  client what it sent, as it does for any field.
- **A CIF made valid by padding:** only a DNI's and a NIE's numbers are padded; a CIF missing a
  character is refused.

## Updating the data

The countries come from ISO 3166-1 through Debian's
[iso-codes](https://salsa.debian.org/iso-codes-team/iso-codes) (4.16.0), and the provinces'
ISO codes are checked against its ISO 3166-2. When ISO changes a code, a release of Alcazaba
brings it: `scripts/generate_countries.py` writes the table again, and `--check` compares it,
and Spain's codes, with the data installed.
