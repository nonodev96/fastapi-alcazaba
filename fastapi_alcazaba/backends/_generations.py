"""Clearing a namespace or a tag without listing keys, for backends that can't list them
(memcached, DynamoDB): generations.

Each namespace or tag that was ever cleared has a generation, a random token under a key of its
own. An entry is stored with the generations of its namespaces and tags (its stamp) and is alive
while they all still match; a clear writes a new token, and every entry stored before it stops
matching at once and expires in its own time.
"""

import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from fastapi_alcazaba.backends.base import is_state_key

#: Namespaces a key belongs to are those before each of its first MAX_DEPTH colons.
MAX_DEPTH = 8
#: Generations are state: no namespace of their own, so no clear hides them.
_GENERATION = "fastapi_alcazaba!generation:"
MAGIC = b"\x00alcazaba-stamp:1\n"


def namespaces(key: str) -> list[str]:
    """The namespaces `key` belongs to, as `clear(namespace=...)` names them: "app:ns:abc" belongs
    to "app" and "app:ns". None for state keys, which clears never touch."""
    if is_state_key(key):
        return []
    found: list[str] = []
    end = key.find(":")
    while end != -1 and len(found) < MAX_DEPTH:
        found.append(key[:end])
        end = key.find(":", end + 1)
    return found


def check_depth(namespace: str) -> None:
    """Refuse a namespace deeper than those tracked: clearing it would hide nothing."""
    if namespace.count(":") >= MAX_DEPTH:
        raise ValueError(f"only namespaces with fewer than {MAX_DEPTH} ':' can be cleared")


def namespace_generation(namespace: str) -> str:
    return f"{_GENERATION}namespace:{namespace}"


def tag_generation(tag: str) -> str:
    return f"{_GENERATION}tag:{tag}"


def new_token() -> str:
    return secrets.token_hex(8)


@dataclass(frozen=True, slots=True)
class Stamp:
    #: Unix time the entry expires at; 0 when it doesn't.
    expires_at: int
    #: Generation key -> the token it had when the entry was stored ("" for none).
    generations: dict[str, str] = field(default_factory=dict)


def encode_stamp(stamp: Stamp, value: bytes) -> bytes:
    meta = {"e": stamp.expires_at, "g": stamp.generations}
    return MAGIC + json.dumps(meta, separators=(",", ":")).encode() + b"\n" + value


def parse_stamp(data: bytes) -> tuple[Stamp, bytes] | None:
    """The stamp and the value of `data`; None when it has no stamp (written by another version or
    another app). ValueError when the stamp is malformed."""
    if not data.startswith(MAGIC):
        return None
    line, newline, value = data[len(MAGIC) :].partition(b"\n")
    if not newline:
        raise ValueError("the stamp has no end")
    try:
        meta = json.loads(line)  # ValueError when it isn't JSON
    except RecursionError as exc:
        raise ValueError("the stamp is nested too deep") from exc
    if not isinstance(meta, dict) or set(meta) != {"e", "g"}:
        raise ValueError("the stamp isn't an object with e and g")
    expires_at, generations = meta["e"], meta["g"]
    if type(expires_at) is not int or expires_at < 0:
        raise ValueError("the stamp's expiry isn't a Unix time")
    if not isinstance(generations, dict) or not all(
        isinstance(token, str) for token in generations.values()
    ):
        raise ValueError("the stamp's generations aren't tokens")
    return Stamp(expires_at, generations), value


def alive(stamp: Stamp, current: Mapping[str, str | None], required: Iterable[str]) -> bool:
    """Whether every generation `stamp` recorded, and every one `required` (the key's namespaces),
    still has the token it had: "" and absent both mean none."""
    keys = {*required, *stamp.generations}
    return all(stamp.generations.get(key, "") == (current.get(key) or "") for key in keys)
