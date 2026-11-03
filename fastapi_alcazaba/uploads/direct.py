"""Files uploaded straight to S3 (by an `upload_form` or an `upload_url`), checked as strictly as
those that go through the app: `check_uploaded` reads the object back, runs `FastAPIUploads`'s
checks on it (its scanner too), and deletes it when it fails them. Downloads with httpx2:
pip install "fastapi-alcazaba[uploads]".

    @app.post("/scans/{scan_id}/done")
    async def scanned(scan_id: int) -> Scan:
        checked = await check_uploaded(uploads, s3, f"scans/{scan_id}")
        ...  # checked.content_type, checked.size; the object stays in the bucket
"""

import logging
import posixpath
from tempfile import SpooledTemporaryFile
from typing import Any

from fastapi import HTTPException, UploadFile
from starlette.status import HTTP_404_NOT_FOUND, HTTP_502_BAD_GATEWAY

from fastapi_alcazaba.uploads.checks import CheckedFile, FastAPIUploads
from fastapi_alcazaba.uploads.s3 import S3Presigner

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover  # httpx under its old name
    try:
        import httpx  # type: ignore[no-redef,import-not-found]
    except ImportError as exc:
        raise ImportError(
            'check_uploaded needs httpx2: pip install "fastapi-alcazaba[uploads]"'
        ) from exc

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Bytes kept in memory while an object is read back; the rest goes to a temporary file.
_IN_MEMORY = 1024 * 1024


async def check_uploaded(
    uploads: FastAPIUploads,
    presigner: S3Presigner,
    key: str,
    *,
    filename: str | None = None,
    client: "httpx.AsyncClient | None" = None,
    timeout: float = 30.0,
) -> CheckedFile:
    """The object `key`, read back from the bucket and checked by `uploads` (type, size,
    pixels, scanner); when it fails, it's deleted and the HTTPException goes on (413, 415, 422).
    When it couldn't be scanned (503), it stays: check it again later. A 404 when there's no
    such object, a 502 when the service doesn't answer as it should.

    Up to `uploads.max_size` + 1 bytes are read: more is "too large" without reading the rest.
    The returned `CheckedFile`'s file holds the bytes read (in memory up to 1 MiB, then on
    disk): close it (`await checked.file.close()`) when done.

    :param filename: the name to check and show (`safe_filename`); the key's last part by
        default.
    :param client: the httpx2 client to call the service with; one of its own by default.
    """
    own = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=timeout)
    # Not in a `with`: it's the returned CheckedFile's, closed here only when there's none.
    spooled: Any = SpooledTemporaryFile(max_size=_IN_MEMORY)  # noqa: SIM115  # pylint: disable=consider-using-with
    try:
        size = await _download(http, presigner.download_url(key, expires=60), spooled, uploads)
        spooled.seek(0)
        upload = UploadFile(spooled, size=size, filename=filename or posixpath.basename(key))
        try:
            return await uploads.check(upload)
        except HTTPException as exc:
            if exc.status_code != 503:  # refused, not merely unscanned: off the bucket
                await _delete(http, presigner.delete_url(key), key)
            raise
    except BaseException:
        spooled.close()
        raise
    finally:
        if own:
            await http.aclose()


async def _download(http: Any, url: str, into: Any, uploads: FastAPIUploads) -> int:
    limit = uploads.max_size + 1  # one byte more tells "too large"
    size = 0
    try:
        async with http.stream("GET", url) as response:
            if response.status_code == 404:
                raise HTTPException(HTTP_404_NOT_FOUND, "No such upload")
            if response.status_code != 200:
                raise HTTPException(HTTP_502_BAD_GATEWAY, "The storage didn't answer")
            async for chunk in response.aiter_bytes():
                chunk = chunk[: limit - size]
                into.write(chunk)
                size += len(chunk)
                if size >= limit:
                    break
    except httpx.HTTPError as exc:
        logger.warning("Couldn't read an upload back from the bucket", exc_info=exc)
        raise HTTPException(HTTP_502_BAD_GATEWAY, "The storage didn't answer") from None
    return size


async def _delete(http: Any, url: str, key: str) -> None:
    try:
        response = await http.delete(url)
        if response.status_code not in (200, 204):
            raise httpx.HTTPError(f"HTTP {response.status_code}")
    except httpx.HTTPError:
        logger.error("Couldn't delete the refused upload %r from the bucket", key, exc_info=True)
        return
    logger.info("Deleted the refused upload %r from the bucket", key)
