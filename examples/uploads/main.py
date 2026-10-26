"""Avatars and documents uploaded, checked by their bytes, stored and served back safely.

    uvicorn examples.uploads.main:app

- `POST /avatars`: one image (PNG, JPEG, GIF or WebP) of up to 2 MiB and 16 million pixels,
  checked by `FastAPIUploads` as a dependency.
- `POST /documents`: up to five PDF, Word, Excel or PowerPoint files of up to 10 MiB each, all
  checked with `check()` before any of them is stored.
- `GET /files/{file_id}`: a file back, with the type found in its bytes, never the one the
  client said, and `X-Content-Type-Options: nosniff`. Images inline; documents as attachments,
  under the client's name made safe, with the extension of what the file really is.

A file is judged by its bytes, not by its name or `Content-Type`: an HTML page sent as `cat.png`
gets a 415. Files are stored under names of the app's own. `BodySizeLimitMiddleware` gives each
route a limit, so that the multipart parser doesn't store a huge body before the checks run.
"""

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, BinaryIO
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from fastapi_alcazaba.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from fastapi_alcazaba.uploads import (
    IMAGES,
    OFFICE_DOCUMENTS,
    CheckedFile,
    FastAPIUploads,
    safe_filename,
)

MIB = 1024 * 1024
MAX_DOCUMENTS = 5
avatars = FastAPIUploads(types=IMAGES, max_size=2 * MIB, max_pixels=16_000_000)
# Without the macro-enabled Office types (.docm, .xlsm, .pptm): OFFICE_DOCUMENTS leaves them out.
documents = FastAPIUploads(types={"application/pdf", *OFFICE_DOCUMENTS}, max_size=10 * MIB)


class StoredFile(BaseModel):
    id: UUID
    key: str = Field(exclude=True)  # where it's stored: its id and the extension of its type
    name: str  # the client's, made safe: for display only
    content_type: str  # found in its bytes
    size: int
    width: int | None = None
    height: int | None = None


class Storage:
    """Files in a directory, under names of the app's own. Their details are kept in memory,
    where a real app has a database table (and maybe object storage, such as S3, for the
    files)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.files: dict[UUID, StoredFile] = {}

    async def save(self, upload: CheckedFile) -> StoredFile:
        file_id = uuid4()
        stored = StoredFile(
            id=file_id,
            key=f"{file_id}{upload.extension}",  # never the client's name
            name=upload.filename,
            content_type=upload.content_type,
            size=upload.size,
            width=upload.width,
            height=upload.height,
        )
        await run_in_threadpool(self._write, self.directory / stored.key, upload.file.file)
        self.files[file_id] = stored
        return stored

    @staticmethod
    def _write(path: Path, source: BinaryIO) -> None:
        with path.open("xb") as target:  # "x": never overwrite a file
            shutil.copyfileobj(source, target)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    with ExitStack() as stack:
        # UPLOAD_DIR in production: a volume that every process shares. For local development, a
        # throwaway directory, deleted at shutdown.
        directory = os.environ.get("UPLOAD_DIR") or stack.enter_context(
            tempfile.TemporaryDirectory(prefix="uploads-")
        )
        application.state.storage = Storage(Path(directory))
        yield


app = FastAPI(lifespan=lifespan)
# Uploads only where they're expected, with room for the multipart framing around the files.
app.add_middleware(
    BodySizeLimitMiddleware,
    max_size=64 * 1024,
    paths={"/avatars": 2 * MIB + 16 * 1024, "/documents": MAX_DOCUMENTS * 10 * MIB + 64 * 1024},
)
app.add_middleware(SecurityHeadersMiddleware)  # last, the outermost: the 413s get the headers too


def get_storage(request: Request) -> Storage:
    storage: Storage = request.app.state.storage
    return storage


StorageDep = Annotated[Storage, Depends(get_storage)]


@app.post("/avatars", status_code=status.HTTP_201_CREATED)
async def upload_avatar(
    upload: Annotated[CheckedFile, Depends(avatars)],
    storage: StorageDep,
) -> StoredFile:
    """One image, in the form field `file`: 415 if it isn't one, 413 if it's too large."""
    return await storage.save(upload)


@app.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_documents(
    files: Annotated[list[UploadFile], File()],
    storage: StorageDep,
) -> list[StoredFile]:
    """Several documents, in the form field `files`: all checked before any is stored, so that
    one bad file stores none."""
    if len(files) > MAX_DOCUMENTS:
        raise HTTPException(422, f"At most {MAX_DOCUMENTS} files at a time")
    checked = [await documents.check(file) for file in files]
    return [await storage.save(upload) for upload in checked]


@app.get("/files/{file_id}")
async def download(file_id: UUID, storage: StorageDep) -> FileResponse:
    """The file, as the type found in its bytes. A real app checks who may read it."""
    stored = storage.files.get(file_id)
    if stored is None:
        raise HTTPException(404, "File not found")
    # The client's name with the extension of the real type: a Word document sent as
    # `minutes.pdf` is downloaded as `minutes.docx`, and opens in the right program.
    stem = stored.name.rpartition(".")[0] or stored.name
    return FileResponse(
        storage.directory / stored.key,
        media_type=stored.content_type,
        filename=safe_filename(stem + Path(stored.key).suffix),
        # A PDF can carry scripts: the browser saves documents rather than open them.
        content_disposition_type="inline" if stored.content_type in IMAGES else "attachment",
    )


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
