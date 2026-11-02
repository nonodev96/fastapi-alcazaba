"""What the response cache stores when a value isn't all there is to keep: a response the endpoint
returned, the headers it set on the injected response, or its output through the route's response
model. A bare value is still stored as its coder writes it.

    \\x00alcazaba-entry:1\\n
    {"kind": "response", "status": 200, "headers": [["content-type", "image/png"]]}\\n
    <the body, or the coded value, as is>

Readers older than this format can't decode it, and recompute (JSON never starts with a NUL,
pickle starts with \\x80); this format reads theirs as bare values. Headers are filtered when
written and again when read: no entry, whoever wrote it, brings back a cookie or a hop-by-hop
header.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, cast, get_args

from starlette.responses import Response

MAGIC = b"\x00alcazaba-entry:"
VERSION = b"1"

Kind = Literal["response", "value", "rendered"]
_KINDS = frozenset(get_args(Kind))

#: Never stored: a client's session (RFC 9111, section 7.1), the connection's own (section 3.1),
#: what's computed again for each response, and the cache's own headers.
NEVER_STORED = frozenset(
    {
        "set-cookie",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "date",
        "age",
        "etag",
        "cache-status",
    }
)
#: The validators and policy a 304 repeats from the response it stands for (RFC 9110, 15.4.5).
NOT_MODIFIED_HEADERS = frozenset({"cache-control", "expires", "vary", "content-location"})
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

Header = tuple[str, str]


@dataclass(frozen=True)
class Entry:
    kind: Kind
    status: int
    headers: tuple[Header, ...]
    payload: bytes


def _valid_header(name: object, value: object) -> bool:
    return (
        isinstance(name, str)
        and isinstance(value, str)
        and _TOKEN.fullmatch(name) is not None
        and not any(char in value for char in "\r\n\0")
        and all(ord(char) < 256 for char in value)  # latin-1, as HTTP carries it
    )


def storable_headers(headers: Iterable[Header], *, paginated: bool = False) -> tuple[Header, ...]:
    """The headers of `headers` an entry may keep, in order, repeated names included. A paginated
    call's Link is made from each request (`paginate`), never stored."""
    dropped = NEVER_STORED | {"link"} if paginated else NEVER_STORED
    return tuple(
        (name, value)
        for name, value in headers
        if _valid_header(name, value) and name.lower() not in dropped
    )


def decoded_headers(raw: Iterable[tuple[bytes, bytes]]) -> list[Header]:
    """Starlette's raw headers as text: latin-1, as Starlette decodes them, so no byte is lost."""
    return [(name.decode("latin-1"), value.decode("latin-1")) for name, value in raw]


def encode_entry(entry: Entry) -> bytes:
    metadata = {
        "kind": entry.kind,
        "status": entry.status,
        "headers": [list(header) for header in storable_headers(entry.headers)],
    }
    line = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode()
    return b"".join((MAGIC, VERSION, b"\n", line, b"\n", entry.payload))


def parse_entry(data: bytes) -> Entry | None:
    """The entry `data` holds; None when it's a bare value (no envelope). Raises ValueError for an
    envelope of another version or a malformed one."""
    if not data.startswith(MAGIC):
        return None
    version, found, rest = data[len(MAGIC) :].partition(b"\n")
    if not found or version != VERSION:
        raise ValueError("an entry of an unknown format")
    line, found, payload = rest.partition(b"\n")
    if not found:
        raise ValueError("an entry without its metadata")
    try:
        metadata = json.loads(line)
    except (ValueError, RecursionError) as exc:
        raise ValueError("an entry whose metadata isn't JSON") from exc
    if not isinstance(metadata, dict):
        raise ValueError("an entry whose metadata isn't an object")
    kind, status, headers = metadata.get("kind"), metadata.get("status"), metadata.get("headers")
    if kind not in _KINDS:
        raise ValueError("an entry of an unknown kind")
    if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status <= 599:
        raise ValueError("an entry with an invalid status")
    if not isinstance(headers, list) or not all(
        isinstance(header, list) and len(header) == 2 for header in headers
    ):
        raise ValueError("an entry with invalid headers")
    return Entry(
        kind=cast(Kind, kind),
        status=status,
        headers=storable_headers((name, value) for name, value in headers),
        payload=payload,
    )


def build_response(entry: Entry) -> Response:
    """The response a `response` entry stands for: its status, headers and body."""
    response = Response(content=entry.payload, status_code=entry.status)
    response.raw_headers = [
        *((name.encode("latin-1"), value.encode("latin-1")) for name, value in entry.headers),
        (b"content-length", str(len(entry.payload)).encode()),
    ]
    return response
