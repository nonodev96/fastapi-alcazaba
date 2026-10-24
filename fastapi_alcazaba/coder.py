import dataclasses
import datetime
import functools
import json
import pickle  # noqa: S403  # PickleCoder only, see Coder.requires_signing
from collections.abc import Callable
from decimal import Decimal
from typing import Any, ClassVar

import pydantic_core
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, PydanticUserError, TypeAdapter
from starlette.responses import JSONResponse, Response

# Values JSON can't represent natively are stored as {TYPE_KEY: <name>, VALUE_KEY: <text>}. The
# keys are namespaced so that ordinary data with a "_spec_type" key (the marker of fastapi-cache2)
# is not mistaken for one, and an unknown name is left alone instead of raising: with the old
# marker, storing {"_spec_type": "x"} in a cached response made every hit of it fail.
TYPE_KEY = "__fastapi_alcazaba_type__"
VALUE_KEY = "__fastapi_alcazaba_value__"
_TYPE_KEY_BYTES = TYPE_KEY.encode()
# datetime.datetime is a datetime.date.
_MARKED_TYPES = (datetime.date, Decimal)

CONVERTERS: dict[str, Callable[[str], Any]] = {
    "date": datetime.date.fromisoformat,
    "datetime": datetime.datetime.fromisoformat,
    "decimal": Decimal,
}


class JsonEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, datetime.datetime):
            return {TYPE_KEY: "datetime", VALUE_KEY: o.isoformat()}
        if isinstance(o, datetime.date):
            return {TYPE_KEY: "date", VALUE_KEY: o.isoformat()}
        if isinstance(o, Decimal):
            return {TYPE_KEY: "decimal", VALUE_KEY: str(o)}
        if isinstance(o, BaseModel):
            # What jsonable_encoder() returns for a model, without walking the result again.
            return o.model_dump(mode="json", by_alias=True)
        return jsonable_encoder(o)


def needs_markers(value: Any) -> bool:
    """True when `value` holds a date, a datetime or a Decimal outside a Pydantic model: only the
    marker format brings those back as they were (pydantic-core would store them as strings).
    Inside models they're strings either way, validated back with the return annotation."""
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, _MARKED_TYPES):
            return True
        if isinstance(item, str | bytes | int | float | BaseModel) or item is None:
            continue
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list | tuple | set | frozenset):
            pending.extend(item)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in dataclasses.fields(item))
    return False


def object_hook(obj: dict[str, Any]) -> Any:
    if len(obj) == 2 and isinstance(obj.get(VALUE_KEY), str):
        converter = CONVERTERS.get(obj.get(TYPE_KEY))  # type: ignore[arg-type]
        if converter is not None:
            return converter(obj[VALUE_KEY])
    return obj


def type_adapter_for(type_: Any) -> TypeAdapter[Any] | None:
    """None when the value can't be validated as `type_`: responses (the cache holds their body)
    and annotations Pydantic has no schema for."""
    if isinstance(type_, type) and issubclass(type_, Response):
        return None
    try:
        return TypeAdapter(type_)
    except (PydanticUserError, TypeError):
        return None


class Coder:
    """Turns values into bytes for the backend, and back."""

    #: True when decoding bytes written by someone else can run code (pickle). Alcazaba warns
    #: when such a coder is used without `FastAPICache.init(signing_key=...)`, which makes it
    #: refuse any entry it didn't write itself.
    requires_signing: ClassVar[bool] = False

    # Return type -> adapter (or None, see type_adapter_for). Shared by the subclasses.
    _type_adapter_cache: ClassVar[dict[Any, TypeAdapter[Any] | None]] = {}

    @classmethod
    def encode(cls, value: Any) -> bytes:
        raise NotImplementedError

    @classmethod
    def decode(cls, value: bytes) -> Any:
        raise NotImplementedError

    @classmethod
    def adapter(cls, type_: Any) -> TypeAdapter[Any] | None:
        try:
            return cls._type_adapter_cache[type_]
        except KeyError:
            adapter = cls._type_adapter_cache[type_] = type_adapter_for(type_)
            return adapter
        except TypeError:  # unhashable annotation
            return type_adapter_for(type_)

    @classmethod
    def decode_as_type(cls, value: bytes, *, type_: Any | None) -> Any:
        """Decode `value` and validate it as `type_` (the return annotation of the cached
        function), so that a cache hit returns the same kind of object as a miss: a Pydantic
        model or a dataclass, not the dict it was stored as. Raises pydantic.ValidationError when
        the cached data doesn't fit the type (e.g. the model changed since it was cached)."""
        result = cls.decode(value)
        if type_ is None or isinstance(result, Response):
            return result
        adapter = cls.adapter(type_)
        return result if adapter is None else adapter.validate_python(result)


class JsonCoder(Coder):
    """JSON. Values without dates or Decimals outside Pydantic models (the usual case) go through
    pydantic-core, in Rust: some 14 times faster than fastapi-cache2's jsonable_encoder() for a
    list of models, and hits with a return annotation are validated straight from the bytes."""

    @classmethod
    def encode(cls, value: Any) -> bytes:
        if isinstance(value, JSONResponse):
            return bytes(value.body)
        if not needs_markers(value):
            return pydantic_core.to_json(value, fallback=jsonable_encoder)
        return json.dumps(value, cls=JsonEncoder).encode()

    @classmethod
    def decode(cls, value: bytes) -> Any:
        # explicitly decode from UTF-8 bytes first, as otherwise json.loads() will first have to
        # detect the correct UTF- encoding used.
        return json.loads(value.decode(), object_hook=object_hook)

    @classmethod
    def decode_as_type(cls, value: bytes, *, type_: Any | None) -> Any:
        if type_ is not None and _TYPE_KEY_BYTES not in value:
            adapter = cls.adapter(type_)
            if adapter is not None:
                return adapter.validate_json(value)
        return super().decode_as_type(value, type_=type_)


@functools.cache
def _template_response_class() -> type[Response] | None:
    """starlette.templating needs jinja2, an optional dependency since Starlette 1.0."""
    try:
        from starlette.templating import _TemplateResponse
    except ImportError:
        return None
    return _TemplateResponse


class PickleCoder(Coder):
    """Pickles the value. Unpickling runs code chosen by whoever wrote the bytes: only use it with
    a `signing_key` (Alcazaba then refuses entries it didn't sign) and a backend that untrusted
    parties can't write to."""

    requires_signing = True

    @classmethod
    def encode(cls, value: Any) -> bytes:
        template_response = _template_response_class()
        if template_response is not None and isinstance(value, template_response):
            value = value.body
        return pickle.dumps(value)

    @classmethod
    def decode(cls, value: bytes) -> Any:
        return pickle.loads(value)  # noqa: S301  # see requires_signing

    @classmethod
    def decode_as_type(cls, value: bytes, *, type_: Any | None) -> Any:
        # Pickle already produces the correct type on decoding, no point in paying an extra
        # performance penalty for pydantic to discover the same.
        return cls.decode(value)
