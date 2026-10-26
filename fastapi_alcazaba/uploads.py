"""Checking uploaded files with nothing but the standard library: the real type of a file from its
bytes, its size counted while it's read, the dimensions of images (against decompression bombs)
and a name that is safe to use.

The name and the `Content-Type` of an upload are whatever the client chose: an HTML page sent as
`cat.png` would be served back as HTML. `sniff` looks at the bytes instead, and `FastAPIUploads`
refuses what isn't on its list (415), what's too large (413) and images with more pixels than it
is safe to decode.

    uploads = FastAPIUploads(types=IMAGES, max_size=5 * 1024 * 1024, max_pixels=25_000_000)

    @app.post("/avatars")
    async def avatar(upload: Annotated[CheckedFile, Depends(uploads)]) -> dict[str, str]:
        name = f"{uuid4()}{upload.extension}"  # never the client's name
        await storage.save(name, upload.file.file, content_type=upload.content_type)
        return {"name": name}

SVG is XML, and XML (like HTML) can carry scripts that run when a browser opens it: it's told
apart from plain text and never counted as an image.
"""

import codecs
import re
import struct
import unicodedata
import zlib
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass
from typing import TypeVar

from fastapi import HTTPException, UploadFile

from fastapi_alcazaba.metrics import Metrics

T = TypeVar("T")

_PNG = "image/png"
_JPEG = "image/jpeg"
_GIF = "image/gif"
_WEBP = "image/webp"
_PDF = "application/pdf"
_ZIP = "application/zip"
_GZIP = "application/gzip"
_MP4 = "video/mp4"
_QUICKTIME = "video/quicktime"
_TEXT = "text/plain"
_HTML = "text/html"
_XML = "application/xml"
_SVG = "image/svg+xml"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_DOCM = "application/vnd.ms-word.document.macroEnabled.12"
_XLSM = "application/vnd.ms-excel.sheet.macroEnabled.12"
_PPTM = "application/vnd.ms-powerpoint.presentation.macroEnabled.12"

#: Images whose dimensions are read from their headers. SVG isn't one.
IMAGES: frozenset[str] = frozenset({_PNG, _JPEG, _GIF, _WEBP})
#: Word, Excel and PowerPoint documents, without the macro-enabled ones.
OFFICE_DOCUMENTS: frozenset[str] = frozenset({_DOCX, _XLSX, _PPTX})

_EXTENSIONS = {
    _PNG: ".png",
    _JPEG: ".jpg",
    _GIF: ".gif",
    _WEBP: ".webp",
    _PDF: ".pdf",
    _ZIP: ".zip",
    _GZIP: ".gz",
    _MP4: ".mp4",
    _QUICKTIME: ".mov",
    _TEXT: ".txt",
    _HTML: ".html",
    _XML: ".xml",
    _SVG: ".svg",
    _DOCX: ".docx",
    _XLSX: ".xlsx",
    _PPTX: ".pptx",
    _DOCM: ".docm",
    _XLSM: ".xlsm",
    _PPTM: ".pptm",
}
#: Every type `sniff` can return.
KNOWN_TYPES: frozenset[str] = frozenset(_EXTENSIONS)
_HINTS = {
    "image/jpg": "use image/jpeg",
    "text/csv": "CSV files are text: allow text/plain and parse them",
    "application/x-zip-compressed": "use application/zip",
    "application/x-gzip": "use application/gzip",
}

_HEAD_SIZE = 4096  # bytes kept to sniff the type
_CHUNK_SIZE = 65_536
_MP4_BRANDS = {
    **dict.fromkeys(
        [b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash"], _MP4
    ),
    b"M4V ": _MP4,
    b"qt  ": _QUICKTIME,
}
# Control characters, C1 ones included, but tab, line feed, form feed and carriage return.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")
_HTML_TAGS = re.compile(r"<!doctype\s+html|<(?:html|head|body|script|iframe|object|embed)[\s/>]")
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # JPEG start-of-frame markers
_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})  # JPEG markers without a length
_MAX_JPEG_STEPS = 1024
_EOCD = b"PK\x05\x06"  # end of a ZIP's central directory
_MAX_ZIP_ENTRIES = 10_000
_MAX_DIRECTORY = 2 * 1024 * 1024
_MAX_PART = 1024 * 1024  # [Content_Types].xml, compressed
_MAX_XML = 256 * 1024  # and decompressed
_OFFICE_MAIN_PARTS = (
    (b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml", _DOCX),
    (b"application/vnd.ms-word.document.macroenabled.main+xml", _DOCM),
    (b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", _XLSX),
    (b"application/vnd.ms-excel.sheet.macroenabled.main+xml", _XLSM),
    (b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml", _PPTX),
    (b"application/vnd.ms-powerpoint.presentation.macroenabled.main+xml", _PPTM),
)
_WITH_MACROS = {_DOCX: _DOCM, _XLSX: _XLSM, _PPTX: _PPTM}
_WINDOWS_RESERVED = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]|CONIN\$|CONOUT\$", re.IGNORECASE)
_SEPARATORS = re.compile(r"[/\\]")

# A reader of a file that asks for the bytes it needs: it yields (offset, length) and gets them
# back, so the same code reads bytes in memory and an UploadFile spooled to disk.
Probe = Generator[tuple[int, int], bytes, T]


def _run(probe: Probe[T], data: bytes) -> T:
    try:
        offset, length = next(probe)
        while True:
            offset, length = probe.send(data[offset : offset + length])
    except StopIteration as stop:
        result: T = stop.value
        return result


async def _run_file(probe: Probe[T], file: UploadFile, head: bytes, size: int) -> T:
    try:
        offset, length = next(probe)
        while True:
            if len(head) == size or offset + length <= len(head):
                chunk = head[offset : offset + length]
            else:
                await file.seek(offset)
                chunk = await file.read(length)
            offset, length = probe.send(chunk)
    except StopIteration as stop:
        result: T = stop.value
        return result


# region Types


def _binary_kind(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return _PNG
    if head.startswith(b"\xff\xd8\xff"):
        return _JPEG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return _GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _WEBP
    if head.startswith(b"%PDF-"):
        return _PDF
    if head.startswith(b"\x1f\x8b\x08"):
        return _GZIP
    if head[:4] in (b"PK\x03\x04", _EOCD):
        return _ZIP
    if head[4:8] == b"ftyp":
        box = int.from_bytes(head[:4], "big")
        if 16 <= box <= 1024 and box % 4 == 0:
            return _MP4_BRANDS.get(head[8:12])
    return None


def _markup(text: str) -> str:
    """HTML, SVG or other XML when the text starts with a tag: never plain text."""
    start = text.lstrip("﻿ \t\n\r\f")
    if not start.startswith("<"):
        return _TEXT
    lowered = start[:_HEAD_SIZE].lower()
    if _HTML_TAGS.search(lowered):
        return _HTML  # an SVG with a script too
    return _SVG if "<svg" in lowered else _XML


def _text_kind(data: bytes, *, final: bool) -> str | None:
    """The type of UTF-8 text without control characters; None if `data` isn't such text. A
    character cut at the end is fine unless `final`."""
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(data, final)
    except UnicodeDecodeError:
        return None
    if not text or _CONTROL.search(text):
        return None
    return _markup(text)


def _is_text(decoder: codecs.IncrementalDecoder, chunk: bytes, final: bool = False) -> bool:
    try:
        return not _CONTROL.search(decoder.decode(chunk, final))
    except UnicodeDecodeError:
        return False


def _find_entry(directory: bytes, entries: int, name: bytes) -> tuple[int, int, int, int] | None:
    """(flags, method, compressed size, offset of the local header) of the only entry of a ZIP
    central directory called `name` (ASCII case aside); None if it isn't there exactly once."""
    found = None
    position = 0
    for _ in range(entries):
        if directory[position : position + 4] != b"PK\x01\x02" or position + 46 > len(directory):
            return None
        flags, method = struct.unpack_from("<HH", directory, position + 8)
        compressed = struct.unpack_from("<I", directory, position + 20)[0]
        name_length, extra, comment = struct.unpack_from("<HHH", directory, position + 28)
        offset = struct.unpack_from("<I", directory, position + 42)[0]
        if directory[position + 46 : position + 46 + name_length].lower() == name:
            if found is not None:
                return None  # two parts with that name: which one counts depends on the reader
            found = flags, method, compressed, offset
        position += 46 + name_length + extra + comment
    return found


def _inflate(raw: bytes, method: int) -> bytes | None:
    if method == 0:
        return raw if len(raw) <= _MAX_XML else None
    try:
        inflated = zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw, _MAX_XML + 1)
    except zlib.error:
        return None
    return inflated if len(inflated) <= _MAX_XML else None


def _office_kind(content_types: bytes) -> str:
    """The Office type a `[Content_Types].xml` declares for the main part; ZIP if none or
    several. A VBA project makes a document macro-enabled whatever its main part says."""
    lowered = content_types.lower()
    found = {kind for marker, kind in _OFFICE_MAIN_PARTS if marker in lowered}
    if len(found) != 1:
        return _ZIP
    kind = found.pop()
    return _WITH_MACROS.get(kind, kind) if b"vbaproject" in lowered else kind


def _zip_kind(size: int) -> Probe[str]:
    """Office documents from other ZIP archives, by the content types the archive declares, read
    from its central directory as Office does. Anything unusual (ZIP64, encryption, several disks,
    duplicate parts, more than the limits) is just a ZIP archive."""
    span = min(size, 22 + 0xFFFF)  # the end record and the longest comment
    tail = yield size - span, span
    at = tail.rfind(_EOCD)
    if at < 0 or at + 22 > len(tail):
        return _ZIP
    disk, directory_disk, here, entries, directory_size, directory_offset, comment = (
        struct.unpack_from("<HHHHIIH", tail, at + 4)
    )
    if (
        at + 22 + comment != len(tail)
        or disk
        or directory_disk
        or here != entries
        or entries > _MAX_ZIP_ENTRIES
        or directory_size > _MAX_DIRECTORY
        or directory_offset + directory_size > size - span + at
    ):
        return _ZIP
    directory = yield directory_offset, directory_size
    entry = _find_entry(directory, entries, b"[content_types].xml")
    if entry is None:
        return _ZIP
    flags, method, compressed, offset = entry
    if flags & 1 or method not in (0, 8) or compressed > _MAX_PART:
        return _ZIP
    local = yield offset, 30
    if len(local) < 30 or local[:4] != b"PK\x03\x04":
        return _ZIP
    name_length, extra = struct.unpack_from("<HH", local, 26)
    raw = yield offset + 30 + name_length + extra, compressed
    content_types = _inflate(raw, method)
    return _ZIP if content_types is None else _office_kind(content_types)


def sniff(data: bytes) -> str | None:
    """The type of a file from its bytes, whatever its name or the type the client said; None
    when it isn't one of `KNOWN_TYPES`.

    The first bytes are enough (a few dozen; 4096 to tell HTML and SVG from other XML), except to
    tell Office documents from other ZIP archives, which takes the whole file. Text must be UTF-8
    (or ASCII) without control characters throughout `data`, which may end in a cut character.
    CSV is text: it can't be told apart from other text."""
    kind = _binary_kind(data)
    if kind == _ZIP:
        return _run(_zip_kind(len(data)), data)
    return kind if kind is not None else _text_kind(data, final=False)


# region Image dimensions


def _dimensions(width: int, height: int) -> tuple[int, int] | None:
    return (width, height) if width and height else None


def _png_size() -> Probe[tuple[int, int] | None]:
    header = yield 0, 24
    if len(header) < 24 or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", header, 16)
    return _dimensions(width, height)


def _gif_size() -> Probe[tuple[int, int] | None]:
    """The logical screen: the canvas every frame is drawn on."""
    header = yield 0, 10
    if len(header) < 10:
        return None
    width, height = struct.unpack_from("<HH", header, 6)
    return _dimensions(width, height)


def _webp_size() -> Probe[tuple[int, int] | None]:
    header = yield 0, 30
    if len(header) < 30:
        return None
    chunk = header[12:16]
    if chunk == b"VP8X":  # extended: the canvas
        width = int.from_bytes(header[24:27], "little") + 1
        height = int.from_bytes(header[27:30], "little") + 1
    elif chunk == b"VP8L":  # lossless
        if header[20] != 0x2F:
            return None
        bits = int.from_bytes(header[21:25], "little")
        width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif chunk == b"VP8 ":  # lossy
        if header[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(header[26:28], "little") & 0x3FFF
        height = int.from_bytes(header[28:30], "little") & 0x3FFF
    else:
        return None
    return _dimensions(width, height)


def _jpeg_size() -> Probe[tuple[int, int] | None]:
    """The frame header (SOF), found by skipping the segments before it (EXIF, ICC profiles...),
    however far it is."""
    position = 2  # after the start-of-image marker
    for _ in range(_MAX_JPEG_STEPS):
        header = yield position, 9
        if len(header) < 4 or header[0] != 0xFF:
            return None
        marker = header[1]
        if marker == 0xFF:  # a fill byte before the marker
            position += 1
        elif marker in _STANDALONE:
            position += 2
        elif marker in (0xD8, 0xD9, 0xDA):  # another image, the end or the scan: no frame
            return None
        elif marker in _SOF:
            if len(header) < 9:
                return None
            height, width = struct.unpack_from(">HH", header, 5)
            return _dimensions(width, height)
        else:
            length = int.from_bytes(header[2:4], "big")
            if length < 2:
                return None
            position += 2 + length
    return None


_SIZE_PROBES: dict[str, Callable[[], Probe[tuple[int, int] | None]]] = {
    _PNG: _png_size,
    _GIF: _gif_size,
    _WEBP: _webp_size,
    _JPEG: _jpeg_size,
}


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG, JPEG, GIF or WebP image, read from its headers without decoding
    it; None for other files and when they can't be read. A JPEG may need more than its first
    bytes: its frame header comes after its metadata."""
    probe = _SIZE_PROBES.get(_binary_kind(data) or "")
    return None if probe is None else _run(probe(), data)


# region Names


def safe_filename(name: str | None, *, default: str = "file", max_length: int = 255) -> str:
    """A version of a client's file name that's safe to show, and to use on any file system:
    NFKC-normalised, only what follows the last `/` or `\\`, without control or invisible
    characters (such as the right-to-left override that disguises `exe.txt` as `txt.exe`), with
    `<>:"|?*` replaced by `_`, without leading dots, spaces or dashes (hidden files, command-line
    options) nor trailing dots and spaces (which Windows drops), `_` before the names Windows
    reserves (CON, NUL, COM1...), and shortened to `max_length` bytes of UTF-8, keeping the
    extension. `default` when nothing is left.

    It doesn't make the extension match the content: store files under a name of your own with
    `CheckedFile.extension`, and keep this one for display or `Content-Disposition`."""
    if max_length < 32:
        raise ValueError("max_length must be at least 32 bytes")
    text = _SEPARATORS.split(unicodedata.normalize("NFKC", name or ""))[-1]
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):
            continue
        kept.append(" " if category == "Zs" else "_" if char in '<>:"|?*' else char)
    text = _shorten("".join(kept), max_length - 1)  # room for the "_" of a reserved name
    text = text.lstrip(". -").rstrip(". ")
    if not text:
        return default
    if _WINDOWS_RESERVED.fullmatch(text.split(".", 1)[0].rstrip(" ")):
        text = "_" + text
    return text


def _shorten(text: str, max_bytes: int) -> str:
    if len(text.encode()) <= max_bytes:
        return text
    stem, dot, extension = text.rpartition(".")
    suffix = dot + extension
    if not stem or len(suffix) > 16:
        stem, suffix = text, ""
    budget = max_bytes - len(suffix.encode())
    # Cut on a character boundary: "ignore" drops the bytes of a character cut in two.
    return stem.encode()[:budget].decode(errors="ignore").rstrip(". ") + suffix


# region Checks


@dataclass(frozen=True, slots=True)
class CheckedFile:
    """An upload that passed `FastAPIUploads`' checks, read back to its start."""

    #: The upload, ready to be read from its start.
    file: UploadFile
    #: The type found in its bytes; not the one the client said.
    content_type: str
    #: Its size in bytes, as counted.
    size: int
    #: `safe_filename` of the client's name: for display, not for storing the file.
    filename: str
    #: Dimensions of an image; None for other files.
    width: int | None = None
    height: int | None = None

    @property
    def extension(self) -> str:
        """The usual extension of its type, with the dot (`.png`)."""
        return _EXTENSIONS[self.content_type]


class FastAPIUploads:
    """Checks uploaded files: their type from their bytes, their size and, for images, their
    dimensions. Use it as a dependency for a form field named `file`, or call `check()`:

        uploads = FastAPIUploads(types={"application/pdf"}, max_size=20 * 1024 * 1024)

        @app.post("/invoices")
        async def invoice(upload: Annotated[CheckedFile, Depends(uploads)]) -> Invoice: ...

        @app.post("/albums")
        async def album(photos: list[UploadFile]) -> Album:
            checked = [await photo_uploads.check(photo) for photo in photos]

    The upload is read in chunks and counted, whatever `UploadFile.size` says. By the time an
    endpoint runs, the multipart parser has already stored the whole request, so keep its size
    down with `BodySizeLimitMiddleware` on the route as well.

    It doesn't look inside the files: a PDF can carry scripts, an Office document macros (the
    macro-enabled types are told apart from the others), ZIP and GZIP archives may expand to far
    more than their size, and the frames of a GIF aren't counted. HTML, SVG and XML run scripts in
    a browser: allow them only to serve them from another domain, as attachments.

    :param types: the types accepted, from `KNOWN_TYPES` (`IMAGES`, `OFFICE_DOCUMENTS`...).
    :param max_size: the largest file accepted, in bytes; larger ones get a 413.
    :param max_pixels: the most pixels (width × height) an image may have; more gets a 413. None
        for no limit. Images whose dimensions can't be read get a 415 either way.
    :param metrics: where to count the outcomes (`fastapi_alcazaba.uploads`); FastAPICache's by
        default.
    """

    def __init__(
        self,
        types: str | Collection[str],
        *,
        max_size: int = 10 * 1024 * 1024,
        max_pixels: int | None = 40_000_000,
        metrics: Metrics | None = None,
    ) -> None:
        accepted = frozenset((types,) if isinstance(types, str) else types)
        if not accepted:
            raise ValueError("give at least one type")
        for kind in sorted(accepted - KNOWN_TYPES):
            hint = _HINTS.get(kind, f"the types that can be told apart are {sorted(KNOWN_TYPES)}")
            raise ValueError(f"{kind!r} can't be detected: {hint}")
        if max_size < 1:
            raise ValueError("max_size must be at least 1 byte")
        if max_pixels is not None and max_pixels < 1:
            raise ValueError("max_pixels must be at least 1 (or None for no limit)")
        self.types = accepted
        self.max_size = max_size
        self.max_pixels = max_pixels
        self._metrics = metrics

    async def __call__(self, file: UploadFile) -> CheckedFile:
        return await self.check(file)

    async def check(self, file: UploadFile) -> CheckedFile:
        """The upload, checked and read back to its start; an HTTPException otherwise: 413 when
        it's larger than `max_size` or has more pixels than `max_pixels`, 415 when its type isn't
        one of `types` or it's an image whose dimensions can't be read, 422 when it's empty."""
        await file.seek(0)
        buffer = bytearray()
        size = 0
        decoder: codecs.IncrementalDecoder | None = codecs.getincrementaldecoder("utf-8")()
        while chunk := await file.read(_CHUNK_SIZE):
            size += len(chunk)
            if size > self.max_size:
                raise self._refuse(413, "File too large", "too_large")
            if len(buffer) < _HEAD_SIZE:
                buffer += chunk[: _HEAD_SIZE - len(buffer)]
            if decoder is not None and not _is_text(decoder, chunk):
                decoder = None
        if not size:
            raise self._refuse(422, "Empty file", "empty")
        head = bytes(buffer)
        kind = _binary_kind(head)
        if kind == _ZIP:
            kind = await _run_file(_zip_kind(size), file, head, size)
        elif kind is None and decoder is not None and _is_text(decoder, b"", final=True):
            kind = _text_kind(head, final=len(head) == size)
        if kind is None or kind not in self.types:
            raise self._refuse(415, "Unsupported file type", "unsupported")
        width = height = None
        probe = _SIZE_PROBES.get(kind)
        if probe is not None:
            dimensions = await _run_file(probe(), file, head, size)
            if dimensions is None:
                raise self._refuse(415, "Unreadable image", "unreadable")
            width, height = dimensions
            if self.max_pixels is not None and width * height > self.max_pixels:
                raise self._refuse(413, "Image too large", "too_many_pixels")
        await file.seek(0)
        metrics = self._metrics_now()
        metrics.count("uploads", outcome="accepted")
        metrics.record("uploads.size", size, content_type=kind)
        return CheckedFile(file, kind, size, safe_filename(file.filename), width, height)

    def _refuse(self, status: int, detail: str, outcome: str) -> HTTPException:
        self._metrics_now().count("uploads", outcome=outcome)
        return HTTPException(status, detail)

    def _metrics_now(self) -> Metrics:
        if self._metrics is not None:
            return self._metrics
        from fastapi_alcazaba import FastAPICache

        return FastAPICache.get_metrics()
