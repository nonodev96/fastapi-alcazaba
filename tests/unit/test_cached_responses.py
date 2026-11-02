"""Whole cached responses: what a hit gives back (status, body, the endpoint's headers), the
Cache-Control it's sent with, and what an entry holds when the route has a response model."""

import pytest

from fastapi_alcazaba.response_cache.entries import (
    MAGIC,
    Entry,
    build_response,
    encode_entry,
    parse_entry,
    storable_headers,
)


class TestEntries:
    @pytest.mark.parametrize("kind", ["response", "value", "rendered"])
    def test_round_trip(self, kind: str) -> None:
        entry = Entry(
            kind=kind,  # type: ignore[arg-type]
            status=200,
            headers=(("content-type", "image/png"), ("x-tag", "a"), ("x-tag", "b")),
            payload=b"\x89PNG\r\n\n\x00 any bytes, newlines included\n",
        )
        assert parse_entry(encode_entry(entry)) == entry

    @pytest.mark.parametrize("data", [b"", b'{"a": 1}', b"\x80\x05pickled", b"plain"])
    def test_bare_values_are_not_entries(self, data: bytes) -> None:
        assert parse_entry(data) is None

    @pytest.mark.parametrize(
        "data",
        [
            MAGIC + b"2\n{}\n",  # another version
            MAGIC + b"1",  # no metadata
            MAGIC + b"1\nnot json\n",
            MAGIC + b'1\n{"kind": "response", "status": 200, "headers": []}',  # no payload line
            MAGIC + b'1\n{"kind": "other", "status": 200, "headers": []}\n',
            MAGIC + b'1\n{"kind": "value", "status": "200", "headers": []}\n',
            MAGIC + b'1\n{"kind": "value", "status": 200, "headers": [["a"]]}\n',
        ],
    )
    def test_malformed_entries(self, data: bytes) -> None:
        with pytest.raises(ValueError, match="entry"):
            parse_entry(data)

    def test_headers_that_are_never_stored(self) -> None:
        headers = [
            ("Set-Cookie", "session=1"),
            ("Connection", "keep-alive"),
            ("Transfer-Encoding", "chunked"),
            ("content-length", "10"),
            ("ETag", '"x"'),
            ("Cache-Status", "Alcazaba; hit"),
            ("Date", "now"),
            ("Link", "</items?page=2>; rel=next"),
            ("X-Total-Count", "2"),
        ]
        assert storable_headers(headers) == (
            ("Link", "</items?page=2>; rel=next"),
            ("X-Total-Count", "2"),
        )
        assert storable_headers(headers, paginated=True) == (("X-Total-Count", "2"),)

    def test_headers_written_to_the_backend_by_someone_else_are_filtered(self) -> None:
        data = (
            MAGIC + b'1\n{"kind": "response", "status": 200, "headers": [["set-cookie", "s=1"],'
            b' ["x-ok", "1"], ["bad name", "x"], ["x-crlf", "a\\r\\nb"], ["x-euro", "\\u20ac"]]}\n'
        )
        entry = parse_entry(data)
        assert entry is not None
        assert entry.headers == (("x-ok", "1"),)

    def test_a_response_is_rebuilt_with_its_length(self) -> None:
        entry = Entry("response", 201, (("content-type", "text/csv"),), b"a,b\n1,2\n")
        response = build_response(entry)
        assert response.status_code == 201
        assert response.body == b"a,b\n1,2\n"
        assert response.raw_headers == [(b"content-type", b"text/csv"), (b"content-length", b"8")]
