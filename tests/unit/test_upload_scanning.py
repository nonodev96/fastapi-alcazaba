"""Scanning uploads (a scanner of the app's, ClamAV's protocol against a fake clamd), presigned
PUTs bounded by size and checksum, and files uploaded straight to the bucket read back."""

import asyncio
import gzip
import hashlib
import io
import logging
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from fastapi import HTTPException, UploadFile
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba.telemetry.metrics import Metrics
from fastapi_alcazaba.uploads import (
    ClamAVScanner,
    FastAPIUploads,
    S3Presigner,
    ScannerUnavailable,
    ScanResult,
)
from fastapi_alcazaba.uploads.direct import check_uploaded
from tests.unit.test_metrics import points

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
TEXT = b"an invoice, in plain text\n"


def upload(data: bytes = TEXT, name: str = "invoice.txt") -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=name)


class FakeScanner:
    def __init__(self, verdict: str = "clean", delay: float = 0) -> None:
        self.verdict = verdict
        self.delay = delay
        self.scanned: list[bytes] = []

    async def scan(self, file: UploadFile) -> ScanResult:
        self.scanned.append(await file.read())
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.verdict == "down":
            raise ScannerUnavailable("down")
        if self.verdict == "infected" or EICAR in self.scanned[-1]:
            return ScanResult(clean=False, threat="Eicar-Test-Signature")
        return ScanResult(clean=True)


def checker(scanner: Any, **options: Any) -> tuple[FastAPIUploads, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
    return FastAPIUploads("text/plain", scanner=scanner, metrics=metrics, **options), reader


def outcomes(reader: InMemoryMetricReader) -> dict[str, int]:
    return {a["fastapi_alcazaba.outcome"]: v for a, v in points(reader, "fastapi_alcazaba.uploads")}


class TestScanning:
    async def test_a_clean_file(self) -> None:
        scanner = FakeScanner()
        uploads, reader = checker(scanner)
        checked = await uploads.check(upload())
        assert scanner.scanned == [TEXT]
        assert await checked.file.read() == TEXT  # rewound for the endpoint
        assert outcomes(reader) == {"accepted": 1}

    @pytest.mark.parametrize("sync", [True, False])
    async def test_an_infected_one(self, sync: bool, caplog: pytest.LogCaptureFixture) -> None:
        quarantined: list[tuple[bytes, str | None]] = []

        def keep(file: UploadFile, result: ScanResult) -> None:
            quarantined.append((file.file.read(), result.threat))

        async def keep_async(file: UploadFile, result: ScanResult) -> None:
            quarantined.append((await file.read(), result.threat))

        uploads, reader = checker(FakeScanner(), on_infected=keep if sync else keep_async)
        with pytest.raises(HTTPException) as refused:
            await uploads.check(upload(TEXT + EICAR))
        assert (refused.value.status_code, refused.value.detail) == (422, "File rejected")
        assert quarantined == [(TEXT + EICAR, "Eicar-Test-Signature")]
        assert outcomes(reader) == {"infected": 1}
        assert "found Eicar-Test-Signature in 'invoice.txt'" in caplog.text

    async def test_on_infected_that_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        def broken(file: UploadFile, result: ScanResult) -> None:
            raise RuntimeError("the quarantine bucket is down")

        uploads, _ = checker(FakeScanner("infected"), on_infected=broken)
        with pytest.raises(HTTPException, match="File rejected"):
            await uploads.check(upload())
        assert "on_infected failed" in caplog.text

    @pytest.mark.parametrize("scanner", [FakeScanner("down"), FakeScanner(delay=1)])
    async def test_fail_closed(self, scanner: FakeScanner) -> None:
        uploads, reader = checker(scanner, scan_timeout=0.05)
        with pytest.raises(HTTPException) as refused:
            await uploads.check(upload())
        assert (refused.value.status_code, refused.value.detail) == (
            503,
            "File scanning unavailable",
        )
        assert outcomes(reader) == {"scan_failed": 1}

    async def test_fail_open(self, caplog: pytest.LogCaptureFixture) -> None:
        uploads, reader = checker(FakeScanner("down"), scan_fail_open=True)
        checked = await uploads.check(upload())
        assert checked.size == len(TEXT)
        assert outcomes(reader) == {"unscanned": 1}
        assert "without a scan" in caplog.text

    async def test_only_what_passed_the_other_checks(self) -> None:
        scanner = FakeScanner()
        uploads, reader = checker(scanner, max_size=10)
        with pytest.raises(HTTPException, match="File too large"):
            await uploads.check(upload())
        with pytest.raises(HTTPException, match="Unsupported"):
            await uploads.check(upload(b"%PDF-1.7\n"))
        assert scanner.scanned == []
        assert outcomes(reader) == {"too_large": 1, "unsupported": 1}

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"scan_timeout": 0}, "scan_timeout"),
            ({"scan_fail_open": True}, "go with a scanner"),
            ({"on_infected": print}, "go with a scanner"),
        ],
    )
    def test_configuration(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            FastAPIUploads("text/plain", **options)


class Clamd:
    """Stands in for clamd: answers PING, and INSTREAM with what `reply` says."""

    def __init__(self, reply: bytes = b"stream: OK") -> None:
        self.reply = reply
        self.received = b""
        self.hang = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        command = await reader.readuntil(b"\0")
        if command == b"zPING\0":
            writer.write(b"PONG\0")
        else:
            while (length := struct.unpack("!I", await reader.readexactly(4))[0]) > 0:
                self.received += await reader.readexactly(length)
            if self.hang:
                await asyncio.sleep(1)
            writer.write(self.reply + b"\0")
        await writer.drain()
        writer.close()


async def serve(clamd: Clamd, tmp_path: Path | None = None) -> tuple[asyncio.AbstractServer, str]:
    if tmp_path is not None:
        path = str(tmp_path / "clamd.sock")
        server = await asyncio.start_unix_server(clamd.handle, path)
        return server, f"unix://{path}"
    server = await asyncio.start_server(clamd.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"tcp://127.0.0.1:{port}"


class TestClamAV:
    async def test_clean_and_infected(self) -> None:
        clamd = Clamd()
        server, address = await serve(clamd)
        async with server:
            scanner = ClamAVScanner(address)
            await scanner.ping()
            data = TEXT * 10_000  # several chunks
            assert await scanner.scan(upload(data)) == ScanResult(clean=True)
            assert clamd.received == data
            clamd.reply = b"stream: Eicar-Test-Signature FOUND"
            assert await scanner.scan(upload()) == ScanResult(False, "Eicar-Test-Signature")

    async def test_a_unix_socket(self, tmp_path: Path) -> None:
        server, address = await serve(Clamd(), tmp_path)
        async with server:
            assert (await ClamAVScanner(address).scan(upload())).clean
            assert (await ClamAVScanner(address.removeprefix("unix://")).scan(upload())).clean

    @pytest.mark.parametrize(
        "reply", [b"INSTREAM size limit exceeded. ERROR", b"stream: something else"]
    )
    async def test_errors(self, reply: bytes) -> None:
        server, address = await serve(Clamd(reply))
        async with server:
            with pytest.raises(ScannerUnavailable, match="couldn't scan"):
                await ClamAVScanner(address).scan(upload())

    async def test_down_or_slow(self) -> None:
        with pytest.raises(ScannerUnavailable, match="didn't answer"):
            await ClamAVScanner("tcp://127.0.0.1:9", timeout=1).scan(upload())
        clamd = Clamd()
        clamd.hang = True
        server, address = await serve(clamd)
        async with server:
            with pytest.raises(ScannerUnavailable, match="didn't answer"):
                await ClamAVScanner(address, timeout=0.1).scan(upload())

    async def test_a_ping_answered_otherwise(self) -> None:
        class Odd(Clamd):
            async def handle(
                self,
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.readuntil(b"\0")
                writer.write(b"PANG\0")
                await writer.drain()
                writer.close()

        server, address = await serve(Odd())
        async with server:
            with pytest.raises(ScannerUnavailable, match="PING"):
                await ClamAVScanner(address).ping()

    @pytest.mark.parametrize("address", ["tcp://clamav", "http://clamav:3310", "tcp://:3310"])
    def test_addresses(self, address: str) -> None:
        with pytest.raises(ValueError, match="address|TCP"):
            ClamAVScanner(address)

    def test_timeout(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ClamAVScanner("tcp://clamav:3310", timeout=0)


def presigner() -> S3Presigner:
    return S3Presigner(
        bucket="uploads",
        region="us-east-1",
        access_key="AKIDEXAMPLE",
        secret_key="the-secret-of-the-tests",  # noqa: S106  # the tests' own
        endpoint="https://s3.example.com",
    )


class TestPresignedPuts:
    def test_size_and_checksum_signed(self) -> None:
        digest = "a" * 64
        put = presigner().upload_url("scans/1", size=42, sha256=digest, content_type="text/plain")
        query = {k: v[0] for k, v in parse_qs(urlsplit(put.url).query).items()}
        assert (
            query["X-Amz-SignedHeaders"] == "content-length;content-type;host;x-amz-checksum-sha256"
        )
        assert put.headers == {
            "content-length": "42",
            "x-amz-checksum-sha256": "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqo=",
            "content-type": "text/plain",
        }
        other = presigner().upload_url("scans/1", size=43)
        assert parse_qs(urlsplit(other.url).query)["X-Amz-Signature"] != [query["X-Amz-Signature"]]
        assert (
            presigner().upload_url("a", size=1, sha256=bytes(32)).headers["x-amz-checksum-sha256"]
        )

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"size": -1}, "length"),
            ({"size": True}, "length"),
            ({"size": 1, "sha256": "ab"}, "SHA-256"),
        ],
    )
    def test_invalid(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            presigner().upload_url("a", **options)

    def test_copy_url(self) -> None:
        copy = presigner().copy_url(
            "incoming/a b", "checked/1", if_match='"abc"', content_type="image/png"
        )
        assert copy.url.startswith("https://s3.example.com/uploads/checked/1?")
        assert copy.headers == {
            "x-amz-copy-source": "/uploads/incoming/a%20b",
            "x-amz-copy-source-if-match": '"abc"',
            "x-amz-metadata-directive": "REPLACE",
            "content-type": "image/png",
        }
        signed = parse_qs(urlsplit(copy.url).query)["X-Amz-SignedHeaders"]
        assert signed == [
            "content-type;host;x-amz-copy-source;x-amz-copy-source-if-match;"
            "x-amz-metadata-directive"
        ]
        with pytest.raises(ValueError, match="ETag"):
            presigner().copy_url("a", "b", if_match="", content_type="text/plain")

    def test_delete_url(self) -> None:
        url = presigner().delete_url("scans/1")
        assert url.startswith("https://s3.example.com/uploads/scans/1?")
        assert parse_qs(urlsplit(url).query)["X-Amz-SignedHeaders"] == ["host"]


class Bucket:
    """Stands in for the bucket: GETs (with an ETag), conditional copies and DELETEs."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.types: dict[str, str] = {}
        self.encodings: dict[str, str] = {}  # the Content-Encoding an uploader set
        self.deleted: list[str] = []
        self.status = 200
        self.delete_status = 204
        self.copy_answer: httpx.Response | None = None
        self.after_read: bytes | None = None  # what the client uploads once it was read

    @staticmethod
    def etag(data: bytes) -> str:
        return f'"{hashlib.md5(data).hexdigest()}"'  # noqa: S324  # S3's ETag of a simple upload

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.removeprefix("/uploads/")
        if request.method == "DELETE":
            self.deleted.append(key)
            self.objects.pop(key, None)
            return httpx.Response(self.delete_status)
        if request.method == "PUT":
            if self.copy_answer is not None:
                return self.copy_answer
            source = request.headers["x-amz-copy-source"].removeprefix("/uploads/")
            if self.etag(self.objects[source]) != request.headers["x-amz-copy-source-if-match"]:
                return httpx.Response(412)
            self.objects[key] = self.objects[source]
            self.types[key] = request.headers["content-type"]
            return httpx.Response(200, content=b"<CopyObjectResult/>")
        if self.status != 200:
            return httpx.Response(self.status)
        if key not in self.objects:
            return httpx.Response(404)
        data = self.objects[key]
        if self.after_read is not None:
            self.objects[key] = self.after_read
        headers = {"etag": self.etag(data)}
        if key in self.encodings:
            headers["content-encoding"] = self.encodings[key]
        # A stream, as from the network: read raw, never decoded.
        return httpx.Response(200, stream=httpx.ByteStream(data), headers=headers)


def client_of(bucket: Bucket) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(bucket.handler))


class TestCheckUploaded:
    async def test_a_good_one_is_moved(self) -> None:
        bucket = Bucket({"scans/1": TEXT})
        uploads = FastAPIUploads("text/plain", scanner=FakeScanner())
        checked = await check_uploaded(
            uploads, presigner(), "scans/1", move_to="checked/1", client=client_of(bucket)
        )
        assert (checked.content_type, checked.size, checked.filename) == (
            "text/plain",
            len(TEXT),
            "1",
        )
        assert await checked.file.read() == TEXT
        await checked.file.close()  # the caller's
        assert bucket.objects == {"checked/1": TEXT}
        assert bucket.types == {"checked/1": "text/plain"}  # the type found, not the uploader's

    async def test_checked_as_stored_whatever_its_content_encoding(self) -> None:
        """An uploader that sets `Content-Encoding: gzip` gets its gzip checked, not the text it
        unpacks to: what would be moved is what was checked."""
        bucket = Bucket({"scans/1": gzip.compress(TEXT)})
        bucket.encodings["scans/1"] = "gzip"
        with pytest.raises(HTTPException) as refused:
            await check_uploaded(
                FastAPIUploads("text/plain", scanner=FakeScanner()),
                presigner(),
                "scans/1",
                move_to="checked/1",
                client=client_of(bucket),
            )
        assert refused.value.status_code == 415
        assert bucket.objects == {}

    async def test_replaced_after_it_was_read(self) -> None:
        """A presigned URL works until it expires: an object replaced once checked is never
        moved."""
        bucket = Bucket({"scans/1": TEXT})
        bucket.after_read = EICAR
        with pytest.raises(HTTPException) as refused:
            await check_uploaded(
                FastAPIUploads("text/plain"),
                presigner(),
                "scans/1",
                move_to="checked/1",
                client=client_of(bucket),
            )
        assert refused.value.status_code == 409
        assert bucket.objects == {}

    @pytest.mark.parametrize(
        "answer",
        [
            httpx.Response(200, content=b"<Error><Code>InternalError</Code></Error>"),
            httpx.Response(500),
        ],
    )
    async def test_a_copy_that_fails(self, answer: httpx.Response) -> None:
        bucket = Bucket({"scans/1": TEXT})
        bucket.copy_answer = answer
        with pytest.raises(HTTPException, match="storage didn't answer"):
            await check_uploaded(
                FastAPIUploads("text/plain"),
                presigner(),
                "scans/1",
                move_to="checked/1",
                client=client_of(bucket),
            )
        assert bucket.objects == {"scans/1": TEXT}  # kept, to check again

    async def test_moved_elsewhere(self) -> None:
        with pytest.raises(ValueError, match="another key"):
            await check_uploaded(
                FastAPIUploads("text/plain"), presigner(), "scans/1", move_to="scans/1"
            )

    @pytest.mark.parametrize(
        ("data", "status"),
        [(b"%PDF-1.7\n", 415), (TEXT + EICAR, 422), (b"x" * 300, 413), (b"", 422)],
    )
    async def test_a_bad_one_is_deleted(self, data: bytes, status: int) -> None:
        bucket = Bucket({"scans/1": data})
        uploads = FastAPIUploads("text/plain", scanner=FakeScanner(), max_size=200)
        with pytest.raises(HTTPException) as refused:
            await check_uploaded(
                uploads,
                presigner(),
                "scans/1",
                move_to="checked/1",
                client=client_of(bucket),
                filename="x.txt",
            )
        assert refused.value.status_code == status
        assert bucket.deleted == ["scans/1"]

    async def test_unscanned_stays(self) -> None:
        bucket = Bucket({"scans/1": TEXT})
        uploads = FastAPIUploads("text/plain", scanner=FakeScanner("down"))
        with pytest.raises(HTTPException, match="unavailable"):
            await check_uploaded(
                uploads, presigner(), "scans/1", move_to="checked/1", client=client_of(bucket)
            )
        assert bucket.deleted == []

    async def test_missing_and_failing_storage(self) -> None:
        uploads = FastAPIUploads("text/plain")
        bucket = Bucket({})
        with pytest.raises(HTTPException, match="No such upload"):
            await check_uploaded(
                uploads, presigner(), "scans/2", move_to="checked/2", client=client_of(bucket)
            )
        bucket.status = 500
        with pytest.raises(HTTPException, match="storage didn't answer"):
            await check_uploaded(
                uploads, presigner(), "scans/2", move_to="checked/2", client=client_of(bucket)
            )

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        refused = httpx.AsyncClient(transport=httpx.MockTransport(broken))
        with pytest.raises(HTTPException, match="storage didn't answer"):
            await check_uploaded(
                uploads, presigner(), "scans/2", move_to="checked/2", client=refused
            )

    async def test_a_delete_that_fails_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO)
        bucket = Bucket({"scans/1": b"%PDF-1.7\n"})
        bucket.delete_status = 403
        with pytest.raises(HTTPException):
            await check_uploaded(
                FastAPIUploads("text/plain"),
                presigner(),
                "scans/1",
                move_to="checked/1",
                client=client_of(bucket),
            )
        assert "Couldn't delete the refused upload 'scans/1'" in caplog.text

    async def test_its_own_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bucket = Bucket({"scans/1": TEXT})
        made: list[httpx.AsyncClient] = []
        original = httpx.AsyncClient

        def make(**options: Any) -> httpx.AsyncClient:
            client = original(transport=httpx.MockTransport(bucket.handler), **options)
            made.append(client)
            return client

        monkeypatch.setattr("fastapi_alcazaba.uploads.direct.httpx.AsyncClient", make)
        checked = await check_uploaded(
            FastAPIUploads("text/plain"), presigner(), "scans/1", move_to="checked/1"
        )
        await checked.file.close()
        assert made[0].is_closed


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_scan_chunks() -> None:
    server, address = await serve(Clamd())
    async with server:
        assert (await ClamAVScanner(address).scan_chunks(_chunks(TEXT))).clean
