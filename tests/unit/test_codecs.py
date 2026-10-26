# pylint: disable=protected-access  # tests look at the internals they check

import datetime
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import JSONResponse

from fastapi_alcazaba import coder as coder_module
from fastapi_alcazaba.coder import JsonCoder, PickleCoder, needs_markers


@dataclass
class DCItem:
    name: str
    price: float
    description: str | None = None
    tax: float | None = None


class PDItem(BaseModel):
    name: str
    price: float
    description: str | None = None
    tax: float | None = None


class NotAModel:
    """No Pydantic schema can be built for this annotation."""


@pytest.mark.parametrize(
    "value",
    [
        1,
        "some_string",
        (1, 2),
        [1, 2, 3],
        {"some_key": 1, "other_key": 2},
        DCItem(name="foo", price=42.0, description="some dataclass item", tax=0.2),
        PDItem(name="foo", price=42.0, description="some pydantic item", tax=0.2),
    ],
)
def test_pickle_coder(value: Any) -> None:
    encoded_value = PickleCoder.encode(value)
    assert isinstance(encoded_value, bytes)
    decoded_value = PickleCoder.decode(encoded_value)
    assert decoded_value == value


@pytest.mark.parametrize(
    ("value", "return_type"),
    [
        (1, None),
        ("some_string", None),
        ((1, 2), tuple[int, int]),
        ([1, 2, 3], None),
        ({"some_key": 1, "other_key": 2}, None),
        (DCItem(name="foo", price=42.0, description="some dataclass item", tax=0.2), DCItem),
        (PDItem(name="foo", price=42.0, description="some pydantic item", tax=0.2), PDItem),
        ([PDItem(name="foo", price=42.0)], list[PDItem]),
        (datetime.datetime(2026, 10, 24, 12, 30, tzinfo=datetime.UTC), None),
        (datetime.date(2026, 10, 24), None),
        (Decimal("10.50"), None),
    ],
)
def test_json_coder(value: Any, return_type: Any) -> None:
    encoded_value = JsonCoder.encode(value)
    assert isinstance(encoded_value, bytes)
    decoded_value = JsonCoder.decode_as_type(encoded_value, type_=return_type)
    assert decoded_value == value


def test_json_coder_validation_error() -> None:
    invalid = b'{"name": "incomplete"}'
    with pytest.raises(ValidationError):
        JsonCoder.decode_as_type(invalid, type_=PDItem)


@pytest.mark.parametrize(
    "data",
    [
        {"_spec_type": "evil", "val": "x"},  # a marker some coders use
        {"__fastapi_alcazaba_type__": "unknown", "__fastapi_alcazaba_value__": "x"},
        {
            "__fastapi_alcazaba_type__": "date",
            "__fastapi_alcazaba_value__": "2026-10-24",
            "extra": 1,
        },
    ],
)
def test_json_coder_leaves_lookalike_markers_alone(data: dict[str, Any]) -> None:
    """User data that looks like a type marker comes back unchanged instead of failing."""
    assert JsonCoder.decode(JsonCoder.encode({"user_data": data})) == {"user_data": data}


@pytest.mark.parametrize("return_type", [NotAModel, JSONResponse])
def test_json_coder_without_schema_returns_plain_data(return_type: Any) -> None:
    assert JsonCoder.decode_as_type(b'{"a": 1}', type_=return_type) == {"a": 1}


def test_json_coder_stores_json_response_body() -> None:
    assert JsonCoder.encode(JSONResponse({"a": 1})) == b'{"a":1}'


def test_pickle_coder_requires_signing() -> None:
    assert PickleCoder.requires_signing
    assert not JsonCoder.requires_signing


def test_coders_work_without_jinja2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starlette 1.0 made jinja2 optional and starlette.templating fails to import without it."""
    monkeypatch.setitem(sys.modules, "starlette.templating", None)
    coder_module._template_response_class.cache_clear()
    try:
        assert PickleCoder.decode(PickleCoder.encode({"a": 1})) == {"a": 1}
    finally:
        coder_module._template_response_class.cache_clear()


class Aliased(BaseModel):
    model_config = {"populate_by_name": False}
    product_name: str = Field(alias="productName")
    created: datetime.datetime


class TestJsonFastPath:
    """Values without dates or Decimals outside models go through pydantic-core."""

    def test_stores_what_jsonable_encoder_stored(self) -> None:
        items = [PDItem(name="a", price=1.5), PDItem(name="b", price=2.0, tax=0.1)]
        assert json.loads(JsonCoder.encode(items)) == jsonable_encoder(items)

    def test_aliases_round_trip(self) -> None:
        value = Aliased(
            productName="oil", created=datetime.datetime(2026, 10, 24, tzinfo=datetime.UTC)
        )
        encoded = JsonCoder.encode(value)
        assert b'"productName"' in encoded
        assert JsonCoder.decode_as_type(encoded, type_=Aliased) == value

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"a": [1, "x", None, 2.5]}, False),
            ([PDItem(name="a", price=1.0)], False),
            ({"when": datetime.date(2026, 1, 1)}, True),
            ([1, (2, {Decimal("1.5")})], True),
            (DCItem(name="a", price=1.0, description=None), False),
            ({"x": [datetime.datetime(2026, 1, 1)]}, True),
        ],
    )
    def test_needs_markers(self, value: Any, expected: bool) -> None:
        assert needs_markers(value) is expected

    def test_dates_inside_dataclasses_keep_the_marker_format(self) -> None:
        @dataclass
        class Event:
            when: datetime.date

        assert needs_markers(Event(datetime.date(2026, 1, 1)))

    def test_cycles_are_not_cached_and_do_not_hang(self) -> None:
        cyclic: list[Any] = [1]
        cyclic.append(cyclic)
        assert not needs_markers(cyclic)
        with pytest.raises(ValueError, match="[Cc]ircular"):
            JsonCoder.encode(cyclic)
