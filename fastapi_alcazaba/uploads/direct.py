"""Files uploaded straight to S3 (by an `upload_form` or an `upload_url`), checked as strictly as
those that go through the app: `check_uploaded` reads the object back, runs `FastAPIUploads`'s
checks on it (its scanner too), and moves it where clients can't write, or deletes it when it
fails them. Downloads with httpx2: pip install "fastapi-alcazaba[uploads]".

    @app.post("/scans/{scan_id}/done")
    async def scanned(scan_id: int) -> Scan:
        checked = await check_uploaded(
            uploads, s3, f"incoming/scans/{scan_id}", move_to=f"scans/{scan_id}"
        )
        ...  # checked.content_type, checked.size; the object is at scans/{scan_id} now

A presigned URL or form works until it expires, as many times as the client likes: checked where
it was uploaded, the object could be replaced right after. So it's moved: copied only if it's
still the one read (its ETag), under the type found in its bytes, to a key the client never got
a URL for.
"""

import logging
import posixpath
from tempfile import SpooledTemporaryFile
from typing import Any

from fastapi import HTTPException, UploadFile
from starlette.status import HTTP_404_NOT_FOUND, HTTP_409_CONFLICT, HTTP_502_BAD_GATEWAY

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
    move_to: str,
    filename: str | None = None,
    client: "httpx.AsyncClient | None" = None,
    timeout: float = 30.0,
) -> CheckedFile:
    """The object `key`, read back from the bucket, checked by `uploads` (type, size, pixels,
    scanner) and moved to `move_to`: copied only while it's still the object read (its ETag),
    with the type found in its bytes as its `Content-Type`, and deleted from `key`. Give
    clients URLs for `key` only, and serve `move_to`: what's there was checked.

    When it fails the checks, it's deleted and the HTTPException goes on (413, 415, 422); when
    it was replaced while it was checked, deleted too, with a 409. When it couldn't be scanned
    (503), it stays at `key`: check it again later. A 404 when there's no such object, a 502
    when the service doesn't answer as it should.

    Up to `uploads.max_size` + 1 bytes are read: more is "too large" without reading the rest.
    They're the stored bytes, whatever `Content-Encoding` the object has: a gzip is checked as
    a gzip.
    The returned `CheckedFile`'s file holds the bytes read (in memory up to 1 MiB, then on
    disk): close it (`await checked.file.close()`) when done.

    :param filename: the name to check and show (`safe_filename`); the key's last part by
        default.
    :param client: the httpx2 client to call the service with; one of its own by default.
    """
    if move_to == key:
        raise ValueError("move_to is another key: one the clients never get a URL for")
    own = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=timeout)
    # Not in a `with`: it's the returned CheckedFile's, closed here only when there's none.
    spooled: Any = SpooledTemporaryFile(max_size=_IN_MEMORY)  # noqa: SIM115  # pylint: disable=consider-using-with
    try:
        size, etag = await _download(
            http, presigner.download_url(key, expires=60), spooled, uploads
        )
        spooled.seek(0)
        upload = UploadFile(spooled, size=size, filename=filename or posixpath.basename(key))
        try:
            checked = await uploads.check(upload)
        except HTTPException as exc:
            if exc.status_code != 503:  # refused, not merely unscanned: off the bucket
                await _delete(http, presigner.delete_url(key), key)
            raise
        copy = presigner.copy_url(key, move_to, if_match=etag, content_type=checked.content_type)
        moved = await _copy(http, copy.url, copy.headers, key)
        await _delete(http, presigner.delete_url(key), key)
        if not moved:
            raise HTTPException(HTTP_409_CONFLICT, "The upload changed while it was checked")
        return checked
    except BaseException:
        spooled.close()
        raise
    finally:
        if own:
            await http.aclose()


async def _download(
    http: Any,
    url: str,
    into: Any,
    uploads: FastAPIUploads,
) -> tuple[int, str]:
    """(bytes read, the object's ETag)."""
    limit = uploads.max_size + 1  # one byte more tells "too large"
    size = 0
    try:
        async with http.stream("GET", url) as response:
            if response.status_code == 404:
                raise HTTPException(HTTP_404_NOT_FOUND, "No such upload")
            etag = response.headers.get("etag")
            if response.status_code != 200 or not etag:
                raise HTTPException(HTTP_502_BAD_GATEWAY, "The storage didn't answer")
            # The bytes as stored, never decoded: an uploader may set a Content-Encoding, and
            # what's checked must be what's copied to move_to (nor may it inflate past `limit`).
            async for chunk in response.aiter_raw():
                chunk = chunk[: limit - size]
                into.write(chunk)
                size += len(chunk)
                if size >= limit:
                    break
    except httpx.HTTPError as exc:
        logger.warning("Couldn't read an upload back from the bucket", exc_info=exc)
        raise HTTPException(HTTP_502_BAD_GATEWAY, "The storage didn't answer") from None
    return size, etag


async def _copy(http: Any, url: str, headers: dict[str, str], key: str) -> bool:
    """Whether the object was copied; False when it was replaced after it was read (what's
    there now wasn't checked)."""
    try:
        response = await http.put(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Couldn't move the checked upload %r", key, exc_info=exc)
        raise HTTPException(HTTP_502_BAD_GATEWAY, "The storage didn't answer") from None
    if response.status_code == 412:
        logger.warning("The upload %r changed while it was checked: deleted", key)
        return False
    # S3 may answer a copy with 200 and an error in the body.
    if response.status_code != 200 or b"<Error>" in response.content:
        logger.warning("Couldn't move the checked upload %r: HTTP %s", key, response.status_code)
        raise HTTPException(HTTP_502_BAD_GATEWAY, "The storage didn't answer")
    return True


async def _delete(http: Any, url: str, key: str) -> None:
    try:
        response = await http.delete(url)
        if response.status_code not in (200, 204):
            raise httpx.HTTPError(f"HTTP {response.status_code}")
    except httpx.HTTPError:
        logger.error("Couldn't delete the refused upload %r from the bucket", key, exc_info=True)
        return
    logger.info("Deleted the refused upload %r from the bucket", key)
