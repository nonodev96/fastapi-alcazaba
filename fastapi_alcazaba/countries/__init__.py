"""Countries: ISO 3166-1's, by any of their codes, and a Pydantic type for a country code; Spain's
provinces, autonomous communities, postal codes and tax IDs in `fastapi_alcazaba.countries.spain`.
Static tables: nothing to set up, no backend."""

from fastapi_alcazaba.countries.iso3166 import (
    COUNTRIES,
    Country,
    CountryCode,
    UnknownCountry,
    country,
)

__all__ = [
    "COUNTRIES",
    "Country",
    "CountryCode",
    "country",
    "UnknownCountry",
]
