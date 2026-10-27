"""Property-based tests (hypothesis) for the parts that parse or transform untrusted data."""

import asyncio
from typing import Any

import fakeredis
from hypothesis import given, settings
from hypothesis import strategies as st

from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.core import Signer
from fastapi_alcazaba.response_cache import JsonCoder
from fastapi_alcazaba.response_cache.decorator import _compute_etag, _etag_matches

json_keys = st.text(max_size=10).filter(lambda key: not key.startswith("__fastapi_alcazaba"))
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(json_keys, children, max_size=5)
    ),
    max_leaves=20,
)


@given(json_values)
def test_json_coder_round_trip(value: Any) -> None:
    assert JsonCoder.decode(JsonCoder.encode(value)) == value


@given(
    st.binary(min_size=1), st.lists(st.text(alphabet="abcdef0123456789", min_size=1), max_size=3)
)
def test_etag_matching(payload: bytes, others: list[str]) -> None:
    etag = _compute_etag(payload)
    unrelated = [f'"{other}"' for other in others if f'W/"{other}"' != etag]
    assert _etag_matches(", ".join([*unrelated, etag]), etag)
    assert _etag_matches(etag.removeprefix("W/"), etag)
    assert not _etag_matches(", ".join(unrelated), etag) or not unrelated


@given(st.binary(), st.text(), st.integers(min_value=0), st.integers(min_value=0, max_value=255))
def test_any_tampering_is_detected(payload: bytes, key: str, position: int, flip: int) -> None:
    signer = Signer("k" * 32)
    signed = bytearray(signer.sign(key, payload))
    index = position % len(signed)
    signed[index] ^= flip or 1
    assert signer.unsign(key, bytes(signed)) is None


@settings(max_examples=50, deadline=None)
@given(st.text(min_size=1, max_size=12), st.lists(st.text(max_size=12), max_size=5))
def test_clear_namespace_deletes_exactly_its_keys(namespace: str, others: list[str]) -> None:
    async def scenario() -> None:
        redis = fakeredis.FakeAsyncRedis()
        own = [f"{namespace}:{i}" for i in range(3)]
        foreign = [key for key in others if not key.startswith(f"{namespace}:")]
        for key in own + foreign:
            await redis.set(key, b"x")
        deleted = await RedisBackend(redis, scan_count=2).clear(namespace=namespace)
        assert deleted == len(own)
        remaining = {key.decode() if isinstance(key, bytes) else key for key in await redis.keys()}
        assert remaining == set(foreign)

    asyncio.run(scenario())
