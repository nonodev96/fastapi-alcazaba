"""Whole cached responses: what a hit gives back (status, body, the endpoint's headers), the
Cache-Control it's sent with, and what an entry holds when the route has a response model."""

import pickle  # noqa: S403  # what PickleCoder raises
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.background import BackgroundTask
from starlette.testclient import TestClient

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.response_cache import Coder, JsonCoder, PickleCoder, cache
from fastapi_alcazaba.response_cache.entries import (
    MAGIC,
    Entry,
    build_response,
    encode_entry,
    parse_entry,
    storable_headers,
)
from tests.cache_status import served

KEY = "k" * 32
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


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


def stored(backend: InMemoryBackend) -> list[bytes]:
    return [value.data for value in backend._store.values()]  # pylint: disable=protected-access


class TestHitsReturnTheResponse:
    """User Story 1: status, body and the endpoint's headers come back on a hit."""

    def _app(self, calls: list[str]) -> FastAPI:
        app = FastAPI()

        @app.get("/png")
        @cache(expire=60)
        async def png() -> Response:
            calls.append("png")
            return Response(PNG, media_type="image/png", headers={"X-Variant": "large"})

        @app.get("/csv")
        @cache(expire=60)
        async def csv() -> Response:
            calls.append("csv")
            return Response("a,b\n1,2\n", media_type="text/csv")

        @app.get("/html")
        @cache(expire=60)
        async def html() -> HTMLResponse:
            calls.append("html")
            return HTMLResponse("<p>hi</p>", headers={"Content-Language": "es"})

        @app.get("/text")
        @cache(expire=60)
        async def text() -> PlainTextResponse:
            calls.append("text")
            return PlainTextResponse("hello")

        @app.get("/json")
        @cache(expire=60)
        async def json_response() -> JSONResponse:
            calls.append("json")
            return JSONResponse([{"id": 1}, {"id": 2}], headers={"X-Total-Count": "2"})

        @app.get("/value")
        @cache(expire=60)
        async def value(response: Response) -> dict[str, int]:
            calls.append("value")
            response.headers["X-Total-Count"] = "2"
            response.headers.append("X-Tag", "a")
            response.headers.append("X-Tag", "b")
            return {"total": 2}

        return app

    @pytest.mark.parametrize("path", ["/png", "/csv", "/html", "/text", "/json", "/value"])
    def test_the_same_response(self, path: str) -> None:
        FastAPICache.init(InMemoryBackend())
        calls: list[str] = []
        with TestClient(self._app(calls)) as client:
            miss = client.get(path)
            hit = client.get(path)
        assert served(miss.headers) == "miss"
        assert served(hit.headers) == "hit"
        assert len(calls) == 1
        assert (hit.status_code, hit.content) == (miss.status_code, miss.content)
        ignored = {"cache-status", "date", "content-length"}  # checked below, wherever it goes
        assert [(k, v) for k, v in hit.headers.multi_items() if k not in ignored] == [
            (k, v) for k, v in miss.headers.multi_items() if k not in ignored
        ]
        assert hit.headers["content-length"] == str(len(hit.content))

    def test_any_coder(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, coder=PickleCoder, signing_key=KEY)
        calls: list[str] = []
        with TestClient(self._app(calls)) as client:
            client.get("/png")
            hit = client.get("/png")
        assert served(hit.headers) == "hit"
        assert hit.content == PNG
        assert hit.headers["x-variant"] == "large"
        # An entry, not a pickled Response, which would hold its background tasks. Signed, not
        # encrypted: the entry is in clear after the signature.
        assert all(MAGIC in data and b"starlette" not in data for data in stored(backend))

    @pytest.mark.parametrize(
        "options",
        [
            {"signing_key": KEY},
            {"encryption_key": KEY},
            {"compression": "zlib", "compression_min_size": 1},
        ],
    )
    def test_signed_encrypted_or_compressed(self, options: dict[str, Any]) -> None:
        FastAPICache.init(InMemoryBackend(), **options)
        calls: list[str] = []
        with TestClient(self._app(calls)) as client:
            client.get("/png")
            hit = client.get("/png")
        assert served(hit.headers) == "hit"
        assert hit.content == PNG

    def test_too_large_to_store(self) -> None:
        FastAPICache.init(InMemoryBackend(), max_value_size=100)
        calls: list[str] = []
        with TestClient(self._app(calls)) as client:
            client.get("/png")
            again = client.get("/png")
        assert served(again.headers) == "miss"
        assert len(calls) == 2


class TestWhatIsNeverStored:
    def test_headers_of_dependencies(self) -> None:
        """Written on the injected response before the endpoint ran: per request, like the
        remaining quota of the accounting meter."""
        FastAPICache.init(InMemoryBackend())
        requests = iter(range(1, 100))

        def quota(response: Response) -> None:
            response.headers["X-Quota-Remaining"] = str(100 - next(requests))

        app = FastAPI()

        @app.get("/item", dependencies=[Depends(quota)])
        @cache(expire=60)
        async def item(response: Response) -> dict[str, int]:
            response.headers["X-Item"] = "1"
            return {"id": 1}

        with TestClient(app) as client:
            client.get("/item")
            hit = client.get("/item")
        assert served(hit.headers) == "hit"
        assert hit.headers.get_list("x-quota-remaining") == ["98"]
        assert hit.headers.get_list("x-item") == ["1"]

    def test_responses_whose_body_isnt_in_memory(self, tmp_path: Path) -> None:
        FastAPICache.init(InMemoryBackend())
        report = tmp_path / "report.csv"
        report.write_text("a,b\n")
        calls: list[str] = []
        app = FastAPI()

        @app.get("/file")
        @cache(expire=60)
        async def file() -> FileResponse:
            calls.append("file")
            return FileResponse(report)

        def chunks() -> Iterator[bytes]:
            yield b"a"

        @app.get("/stream")
        @cache(expire=60)
        async def stream() -> StreamingResponse:
            calls.append("stream")
            return StreamingResponse(chunks())

        with TestClient(app) as client:
            for path in ("/file", "/file", "/stream", "/stream"):
                response = client.get(path)
                assert response.content in (b"a,b\n", b"a")
                assert served(response.headers) == "miss"
        assert calls == ["file", "file", "stream", "stream"]

    def test_background_tasks_run_only_when_the_endpoint_does(self) -> None:
        FastAPICache.init(InMemoryBackend())
        sent: list[str] = []
        app = FastAPI()

        @app.get("/report")
        @cache(expire=60)
        async def report() -> Response:
            return Response(b"done", background=BackgroundTask(sent.append, "mail"))

        with TestClient(app) as client:
            client.get("/report")
            hit = client.get("/report")
        assert served(hit.headers) == "hit"
        assert sent == ["mail"]


class TestOtherVersions:
    def test_bare_values_of_earlier_versions_are_still_served(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend)
        app = FastAPI()

        @app.get("/value")
        @cache(expire=60)
        async def value() -> dict[str, int]:
            return {"fresh": 1}

        with TestClient(app) as client:
            client.get("/value")
            for entry in backend._store.values():  # pylint: disable=protected-access
                entry.data = b'{"stored": 1}'  # what 0.7 wrote
            hit = client.get("/value")
        assert served(hit.headers) == "hit"
        assert hit.json() == {"stored": 1}

    @pytest.mark.parametrize(
        ("coder", "error"),
        [(JsonCoder, ValueError), (PickleCoder, pickle.UnpicklingError)],
    )
    def test_earlier_versions_cant_decode_an_entry(
        self,
        coder: type[Coder],
        error: type[Exception],
    ) -> None:
        """They log it and recompute: what 0.7 does with an entry it can't decode."""
        entry = encode_entry(Entry("response", 200, (), b"{}"))
        with pytest.raises(error):
            coder.decode_as_type(entry, type_=None)
