"""Direct uploads against a real S3 (MinIO) and a real clamd, each enabled by its variable:

    MINIO_URL=http://127.0.0.1:9000     (user and password minioadmin)
    CLAMAV_URL=tcp://127.0.0.1:3310

Presigned PUTs bounded by size and checksum, and objects read back, scanned and deleted. Each
test has a bucket of its own.
"""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest

from fastapi_alcazaba.uploads import ClamAVScanner, FastAPIUploads, S3Presigner
from fastapi_alcazaba.uploads.direct import check_uploaded

pytestmark = pytest.mark.integration

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
TEXT = b"an invoice, in plain text\n"


@pytest.fixture(name="s3")
async def fixture_s3() -> AsyncIterator[S3Presigner]:
    url = os.environ.get("MINIO_URL")
    if not url:
        pytest.skip("MINIO_URL not set")
    from aiobotocore.session import get_session

    bucket = f"alcazaba-{uuid.uuid4().hex[:12]}"
    options: dict[str, Any] = {
        "endpoint_url": url,
        "region_name": "us-east-1",
        "aws_access_key_id": "minioadmin",
        "aws_secret_access_key": "minioadmin",
    }
    async with get_session().create_client("s3", **options) as admin:
        await admin.create_bucket(Bucket=bucket)
        yield S3Presigner(
            bucket=bucket,
            region="us-east-1",
            access_key="minioadmin",
            secret_key="minioadmin",  # noqa: S106  # MinIO's default, of the test server
            endpoint=url,
        )
        listed = await admin.list_objects_v2(Bucket=bucket)
        for item in listed.get("Contents", []):
            await admin.delete_object(Bucket=bucket, Key=item["Key"])
        await admin.delete_bucket(Bucket=bucket)


async def put(s3: S3Presigner, key: str, data: bytes, **options: Any) -> int:
    upload = s3.upload_url(key, **{"size": len(data), **options})
    # What a client that doesn't play along sends: the real length, whatever was signed.
    headers = {**upload.headers, "content-length": str(len(data))}
    async with httpx.AsyncClient() as client:
        response = await client.put(upload.url, content=data, headers=headers)
    return response.status_code


async def test_presigned_puts_are_bounded(s3: S3Presigner) -> None:
    assert await put(s3, "a.txt", TEXT, sha256=hashlib.sha256(TEXT).hexdigest()) == 200
    # Another size than the one signed, another checksum: refused.
    assert await put(s3, "b.txt", TEXT + b"more", size=len(TEXT)) == 403
    wrong = hashlib.sha256(b"something else").hexdigest()
    assert await put(s3, "c.txt", TEXT, sha256=wrong) == 400
    async with httpx.AsyncClient() as client:
        assert (await client.get(s3.download_url("a.txt"))).content == TEXT
        assert (await client.get(s3.download_url("b.txt"))).status_code == 404
        assert (await client.get(s3.download_url("c.txt"))).status_code == 404


async def test_read_back_checked_and_deleted(s3: S3Presigner) -> None:
    scanner = None
    if address := os.environ.get("CLAMAV_URL"):
        scanner = ClamAVScanner(address)
    uploads = FastAPIUploads("text/plain", scanner=scanner)
    # Uploaded as HTML: moved as what it is, text, so it never runs as a page.
    await put(s3, "incoming/good.txt", TEXT, content_type="text/html")
    checked = await check_uploaded(uploads, s3, "incoming/good.txt", move_to="files/good.txt")
    assert checked.size == len(TEXT)
    await checked.file.close()
    await put(s3, "incoming/bad.pdf", b"%PDF-1.7\n")
    with pytest.raises(Exception, match="Unsupported file type"):
        await check_uploaded(uploads, s3, "incoming/bad.pdf", move_to="files/bad.pdf")
    async with httpx.AsyncClient() as client:
        moved = await client.get(s3.download_url("files/good.txt"))
        assert (moved.content, moved.headers["content-type"]) == (TEXT, "text/plain")
        for gone in ("incoming/good.txt", "incoming/bad.pdf", "files/bad.pdf"):
            assert (await client.get(s3.download_url(gone))).status_code == 404
    if scanner is not None:
        await put(s3, "incoming/eicar.txt", EICAR)
        with pytest.raises(Exception, match="File rejected"):
            await check_uploaded(uploads, s3, "incoming/eicar.txt", move_to="files/eicar.txt")
        async with httpx.AsyncClient() as client:
            assert (await client.get(s3.download_url("incoming/eicar.txt"))).status_code == 404


async def test_replaced_after_it_was_read(s3: S3Presigner) -> None:
    """The copy is conditional on the ETag read: MinIO refuses it once the object changed."""
    await put(s3, "incoming/a.txt", TEXT)
    copy = s3.copy_url("incoming/a.txt", "files/a.txt", if_match='"0"', content_type="text/plain")
    async with httpx.AsyncClient() as client:
        assert (await client.put(copy.url, headers=copy.headers)).status_code == 412
        assert (await client.get(s3.download_url("files/a.txt"))).status_code == 404


async def test_clamd_answers(s3: S3Presigner) -> None:
    del s3
    address = os.environ.get("CLAMAV_URL")
    if not address:
        pytest.skip("CLAMAV_URL not set")
    await ClamAVScanner(address).ping()
