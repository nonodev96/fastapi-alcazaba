"""Uploads: the real type from the bytes, sizes, image dimensions and safe file names."""

import gzip
import io
import struct
import warnings
import zipfile
from collections.abc import Mapping
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast

import pytest
from fastapi import HTTPException, UploadFile

from fastapi_alcazaba.uploads import (
    IMAGES,
    KNOWN_TYPES,
    OFFICE_DOCUMENTS,
    CheckedFile,
    FastAPIUploads,
    image_size,
    safe_filename,
    sniff,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCM = "application/vnd.ms-word.document.macroEnabled.12"
XLSM = "application/vnd.ms-excel.sheet.macroEnabled.12"
MAIN = {
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "word-macros": "application/vnd.ms-word.document.macroEnabled.main+xml",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "excel-macros": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    "powerpoint": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
    ),
}


# region Sample files


def png(width: int = 640, height: int = 480, padding: int = 0) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = struct.pack(">I", 13) + b"IHDR" + header + b"\0\0\0\0"
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"\0" * padding


def jpeg(width: int = 800, height: int = 600, metadata: int = 14, before: bytes = b"") -> bytes:
    app1 = b"\xff\xe1" + struct.pack(">H", metadata + 2) + b"\0" * metadata
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\1\x22\0\2\x11\1\3\x11\1"
    scan = b"\xff\xda" + struct.pack(">H", 12) + b"\0" * 10 + b"\xff\xd9"
    return b"\xff\xd8" + before + app1 + sof + scan


def gif(width: int = 32, height: int = 16, version: bytes = b"89a") -> bytes:
    return b"GIF" + version + struct.pack("<HH", width, height) + b"\0\0\0;"


def webp(chunk: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_extended(width: int, height: int) -> bytes:
    size = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return webp(b"VP8X", b"\0\0\0\0" + size)


def webp_lossless(width: int, height: int, signature: int = 0x2F) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return webp(b"VP8L", bytes([signature]) + bits.to_bytes(4, "little") + b"\0" * 5)


def webp_lossy(width: int, height: int, start: bytes = b"\x9d\x01\x2a") -> bytes:
    return webp(b"VP8 ", b"\0\0\0" + start + struct.pack("<HH", width, height))


def mp4(brand: bytes = b"isom", box: int = 24) -> bytes:
    return struct.pack(">I", box) + b"ftyp" + brand + b"\0\0\2\0" + b"isomiso2"


def archive(
    files: Mapping[str, bytes | str],
    method: int = zipfile.ZIP_DEFLATED,
    comment: bytes = b"",
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
        zipped.comment = comment
    return buffer.getvalue()


def content_types(*main: str, extra: str = "") -> str:
    overrides = "".join(f'<Override PartName="/x" ContentType="{MAIN[kind]}"/>' for kind in main)
    return f'<?xml version="1.0"?><Types>{overrides}{extra}</Types>'


def office(*main: str, extra: str = "", files: Mapping[str, bytes] | None = None) -> bytes:
    return archive({"[Content_Types].xml": content_types(*main, extra=extra), **(files or {})})


def patched(data: bytes, offset: int, value: bytes) -> bytes:
    return data[:offset] + value + data[offset + len(value) :]


def directory(data: bytes, entry: int = 0) -> int:
    """Offset of a ZIP's central directory entry."""
    position = -1
    for _ in range(entry + 1):
        position = data.index(b"PK\x01\x02", position + 1)
    return position


# region Sniffing


class TestSniff:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (png(), "image/png"),
            (jpeg(), "image/jpeg"),
            (gif(version=b"87a"), "image/gif"),
            (gif(), "image/gif"),
            (webp_extended(10, 10), "image/webp"),
            (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "application/pdf"),
            (gzip.compress(b"hello"), "application/gzip"),
            (archive({"a.txt": "a"}), "application/zip"),
            (archive({}), "application/zip"),
            (mp4(), "video/mp4"),
            (mp4(b"M4V "), "video/mp4"),
            (mp4(b"qt  "), "video/quicktime"),
            (mp4(b"heic"), None),  # an image whose dimensions can't be read
            (mp4(box=8), None),
            (mp4(box=26), None),
            (b"name,price\nolive oil,9.5\n", "text/plain"),
            ("\ufeffcafé, crème\r\n\tbrûlée\f".encode(), "text/plain"),
            ("caf\u00e9".encode()[:-1], "text/plain"),  # cut in the middle of a character
            (b"a < b", "text/plain"),
            (b"\x00\x01\x02", None),
            (b"text with a NUL\x00", None),
            (b"text with an escape \x1b[2J", None),
            ("C1 control \u009b".encode(), None),
            (b"\xff\xfeu\x00t\x00f\x00", None),  # UTF-16
            (b"", None),
        ],
    )
    def test_types(self, data: bytes, expected: str | None) -> None:
        assert sniff(data) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<!DOCTYPE html><p>hi", "text/html"),
            ("  \n<html lang='en'>", "text/html"),
            ("<div><script>alert(1)</script></div>", "text/html"),
            ('<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>', "text/html"),
            ('<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
            ("\ufeff<SVG/>", "image/svg+xml"),
            ('<?xml version="1.0"?><gpx/>', "application/xml"),
            ("<3 olive oil", "application/xml"),
        ],
    )
    def test_markup_is_never_plain_text(self, text: str, expected: str) -> None:
        assert sniff(text.encode()) == expected

    def test_every_known_type_has_an_extension(self) -> None:
        assert {"image/png", "text/plain", DOCX, DOCM} <= KNOWN_TYPES
        assert IMAGES <= KNOWN_TYPES
        assert OFFICE_DOCUMENTS == {DOCX, XLSX, PPTX}


class TestOfficeDocuments:
    @pytest.mark.parametrize(
        ("main", "expected"),
        [
            ("word", DOCX),
            ("word-macros", DOCM),
            ("excel", XLSX),
            ("excel-macros", XLSM),
            ("powerpoint", PPTX),
        ],
    )
    def test_the_main_part_says_which(self, main: str, expected: str) -> None:
        assert sniff(office(main)) == expected

    def test_a_vba_project_means_macros(self) -> None:
        vba = '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
        assert sniff(office("word", extra=vba)) == DOCM
        assert sniff(office("excel", extra=vba)) == XLSM
        assert sniff(office("word-macros", extra=vba)) == DOCM

    def test_names_are_case_insensitive(self) -> None:
        data = archive({"[CONTENT_TYPES].XML": content_types("word")})
        assert sniff(data) == DOCX

    def test_stored_parts(self) -> None:
        assert sniff(archive({"[Content_Types].xml": content_types("excel")}, 0)) == XLSX

    def test_after_other_entries_and_with_a_comment(self) -> None:
        files = {"a.txt": "a" * 5000, "[Content_Types].xml": content_types("word")}
        assert sniff(archive(files, comment=b"PK\x05\x06 in a comment")) == "application/zip"
        assert sniff(archive(files, comment=b"just a comment")) == DOCX

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(office(), id="no main part"),
            pytest.param(office("word", "excel"), id="two main parts"),
            pytest.param(archive({"word/document.xml": "x"}), id="no content types"),
            pytest.param(
                archive({"[Content_Types].xml": content_types("word") + " " * 300_000}),
                id="content types too large",
            ),
            pytest.param(
                archive({"[Content_Types].xml": content_types("word") + " " * 300_000}, 0),
                id="stored content types too large",
            ),
            pytest.param(office("word")[:100], id="only the first bytes"),
            pytest.param(office("word") + b"trailing", id="data after the end record"),
            pytest.param(b"PK\x03\x04" + b"\0" * 30 + b"PK\x05\x06", id="truncated end record"),
        ],
    )
    def test_anything_else_is_just_a_zip(self, data: bytes) -> None:
        assert sniff(data) == "application/zip"

    def test_duplicate_content_types_are_ambiguous(self) -> None:
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
            with zipfile.ZipFile(buffer, "w") as zipped:
                zipped.writestr("[Content_Types].xml", content_types("word"))
                zipped.writestr("[Content_Types].xml", content_types("word-macros"))
        assert sniff(buffer.getvalue()) == "application/zip"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param(4, b"\1\0", id="another disk"),
            pytest.param(8, b"\2\0", id="entries on this disk"),
            pytest.param(10, struct.pack("<H", 10_001), id="too many entries"),
            pytest.param(12, struct.pack("<I", 3 * 1024 * 1024), id="directory too large"),
            pytest.param(16, struct.pack("<I", 10_000), id="directory past the end"),
        ],
    )
    def test_odd_end_records(self, field: int, value: bytes) -> None:
        data = office("word")
        end = data.rindex(b"PK\x05\x06")
        assert sniff(patched(data, end + field, value)) == "application/zip"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param(0, b"PK\x01\x03", id="bad signature"),
            pytest.param(8, b"\1\0", id="encrypted"),
            pytest.param(10, b"\x0c\0", id="bzip2"),
            pytest.param(20, struct.pack("<I", 2 * 1024 * 1024), id="compressed too large"),
            pytest.param(42, struct.pack("<I", 1), id="local header elsewhere"),
        ],
    )
    def test_odd_directory_entries(self, field: int, value: bytes) -> None:
        data = office("word")
        assert sniff(patched(data, directory(data) + field, value)) == "application/zip"

    def test_a_local_header_near_the_end(self) -> None:
        data = office("word")
        offset = struct.pack("<I", len(data) - 10)
        assert sniff(patched(data, directory(data) + 42, offset)) == "application/zip"

    def test_corrupt_compressed_data(self) -> None:
        data = office("word")
        start = 30 + len("[Content_Types].xml")
        assert sniff(patched(data, start, b"\xff")) == "application/zip"

    def test_a_directory_shorter_than_its_entries(self) -> None:
        data = office("word")
        end = data.rindex(b"PK\x05\x06")
        assert sniff(patched(data, end + 12, struct.pack("<I", 30))) == "application/zip"


# region Image sizes


class TestImageSize:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (png(640, 480), (640, 480)),
            (gif(32, 16), (32, 16)),
            (webp_extended(4000, 3000), (4000, 3000)),
            (webp_lossless(1024, 768), (1024, 768)),
            (webp_lossy(320, 240), (320, 240)),
            (jpeg(800, 600), (800, 600)),
            (jpeg(800, 600, metadata=60_000), (800, 600)),
            (jpeg(1, 2, before=b"\xff\xff\xff\x01\xff\xd0"), (1, 2)),  # fill bytes, standalone
        ],
    )
    def test_dimensions(self, data: bytes, expected: tuple[int, int]) -> None:
        assert image_size(data) == expected

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"%PDF-1.7", id="not an image"),
            pytest.param(png()[:20], id="truncated png"),
            pytest.param(patched(png(), 12, b"IEND"), id="png without IHDR"),
            pytest.param(png(0, 480), id="png without width"),
            pytest.param(gif()[:8], id="truncated gif"),
            pytest.param(gif(16, 0), id="gif without height"),
            pytest.param(webp_extended(10, 10)[:20], id="truncated webp"),
            pytest.param(webp_lossless(10, 10, signature=0), id="webp lossless signature"),
            pytest.param(webp_lossy(10, 10, start=b"\0\0\0"), id="webp lossy start code"),
            pytest.param(webp(b"ALPH", b"\0" * 10), id="webp unknown chunk"),
            pytest.param(webp_lossy(0, 10), id="webp without width"),
            pytest.param(jpeg(0, 600), id="jpeg without width"),
            pytest.param(jpeg(800, 0), id="jpeg with its height later (DNL)"),
            pytest.param(jpeg()[:20], id="truncated jpeg"),
            pytest.param(jpeg()[:28], id="truncated frame header"),
            pytest.param(b"\xff\xd8\x00\x00\x00\x00", id="not a marker"),
            pytest.param(b"\xff\xd8\xff\xda\x00\x08", id="scan before the frame"),
            pytest.param(b"\xff\xd8\xff\xd9\x00\x00", id="end before the frame"),
            pytest.param(b"\xff\xd8\xff\xd8\x00\x00", id="another image"),
            pytest.param(b"\xff\xd8\xff\xe0\x00\x01", id="segment too short"),
            pytest.param(b"\xff\xd8" + b"\xff" * 2000, id="endless fill bytes"),
        ],
    )
    def test_unreadable(self, data: bytes) -> None:
        assert image_size(data) is None


# region File names


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (None, "file"),
            ("", "file"),
            ("photo.jpg", "photo.jpg"),
            ("résumé.pdf", "résumé.pdf"),
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\me\\evil.exe", "evil.exe"),
            ("uploads/", "file"),
            ("ｅｖｉｌ／ｐａｔｈ.txt", "path.txt"),  # fullwidth: NFKC makes the slash a slash
            ("ﬁle.txt", "file.txt"),
            (".htaccess", "htaccess"),
            ("...", "file"),
            ("-rf", "rf"),
            ("  name.txt . ", "name.txt"),
            ("report.pdf.", "report.pdf"),
            ('a<b>c:d"e|f?g*h.txt', "a_b_c_d_e_f_g_h.txt"),
            ("invoice\u202etxt.exe", "invoicetxt.exe"),  # right-to-left override
            ("zero\u200bwidth", "zerowidth"),
            ("tab\there\x00\x7f", "tabhere"),
            ("line\u2028break", "linebreak"),
            ("thin\u2009space", "thin space"),
            ("CON", "_CON"),
            ("con.txt", "_con.txt"),
            ("Com1.tar.gz", "_Com1.tar.gz"),
            ("LPT\u00b9", "_LPT1"),
            ("CONIN$", "_CONIN$"),
            ("CON .txt", "_CON .txt"),
            ("console.txt", "console.txt"),
        ],
    )
    def test_names(self, name: str | None, expected: str) -> None:
        assert safe_filename(name) == expected

    def test_long_names_keep_their_extension(self) -> None:
        name = safe_filename("a" * 300 + ".txt")
        assert (len(name.encode()), name[-4:]) == (254, ".txt")
        accented = safe_filename("é" * 200 + ".pdf")
        assert len(accented.encode()) <= 254
        assert accented.endswith("é.pdf")
        assert "\ufffd" not in accented

    def test_long_names_without_a_short_extension(self) -> None:
        assert safe_filename("a" * 300) == "a" * 254
        assert safe_filename("a" * 300 + "." + "b" * 20) == "a" * 254
        assert safe_filename("." + "a" * 300) == "a" * 253

    def test_dots_left_at_the_cut(self) -> None:
        assert safe_filename("a" * 240 + "." * 20 + ".txt") == "a" * 240 + ".txt"

    def test_options(self) -> None:
        assert safe_filename("...", default="upload.bin") == "upload.bin"
        assert safe_filename("a" * 100 + ".txt", max_length=33) == "a" * 28 + ".txt"
        with pytest.raises(ValueError, match="at least 32"):
            safe_filename("a", max_length=31)


# region Checks


def upload(data: bytes, filename: str | None = "upload.bin", size: int | None = None) -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=filename, size=size)


async def refused(uploads: FastAPIUploads, data: bytes) -> tuple[int, str]:
    with pytest.raises(HTTPException) as caught:
        await uploads.check(upload(data))
    return caught.value.status_code, caught.value.detail


class TestFastAPIUploads:
    async def test_an_accepted_image(self) -> None:
        uploads = FastAPIUploads(IMAGES)
        file = upload(png(640, 480, padding=10), filename="../Holiday photo.PNG")
        await file.read(5)  # wherever it was, it's checked from the start
        checked = await uploads.check(file)
        assert checked == CheckedFile(
            file, "image/png", len(png(padding=10)), "Holiday photo.PNG", 640, 480
        )
        assert checked.extension == ".png"
        assert await file.read() == png(640, 480, padding=10)  # back at the start

    async def test_as_a_dependency(self) -> None:
        checked = await FastAPIUploads("application/pdf")(upload(b"%PDF-1.7\n", filename=None))
        assert (checked.content_type, checked.filename, checked.width) == (
            "application/pdf",
            "file",
            None,
        )
        assert checked.extension == ".pdf"

    async def test_the_size_is_counted(self) -> None:
        uploads = FastAPIUploads("text/plain", max_size=100)
        assert (await uploads.check(upload(b"a" * 100))).size == 100
        assert await refused(uploads, b"a" * 101) == (413, "File too large")
        with pytest.raises(HTTPException) as caught:
            await uploads.check(upload(b"a" * 1000, size=10))  # whatever it says
        assert caught.value.status_code == 413

    async def test_files_spooled_to_disk(self) -> None:
        with SpooledTemporaryFile(max_size=10) as spooled:
            file = UploadFile(cast(BinaryIO, spooled), filename="big.txt")
            await file.write(b"olive oil\n" * 10_000)
            checked = await FastAPIUploads("text/plain", max_size=200_000).check(file)
            assert checked.size == 100_000
            assert await file.read(9) == b"olive oil"

    async def test_types_not_accepted(self) -> None:
        uploads = FastAPIUploads(IMAGES)
        assert await refused(uploads, b"%PDF-1.7\n") == (415, "Unsupported file type")
        assert await refused(uploads, b"\0\1\2\3") == (415, "Unsupported file type")
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        markup = await refused(FastAPIUploads({"text/plain", *IMAGES}), svg)
        assert markup == (415, "Unsupported file type")

    async def test_empty_files(self) -> None:
        assert await refused(FastAPIUploads("text/plain"), b"") == (422, "Empty file")

    async def test_images_whose_dimensions_cant_be_read(self) -> None:
        assert await refused(FastAPIUploads(IMAGES), png()[:20]) == (415, "Unreadable image")

    async def test_decompression_bombs(self) -> None:
        uploads = FastAPIUploads(IMAGES, max_pixels=1_000_000)
        assert (await uploads.check(upload(png(1000, 1000)))).width == 1000
        assert await refused(uploads, png(1001, 1000)) == (413, "Image too large")
        assert await refused(uploads, gif(65535, 65535)) == (413, "Image too large")
        unlimited = FastAPIUploads(IMAGES, max_pixels=None)
        assert (await unlimited.check(upload(gif(65535, 65535)))).height == 65535

    async def test_dimensions_past_the_first_bytes(self) -> None:
        """A JPEG's frame header comes after its metadata, read from the file."""
        uploads = FastAPIUploads(IMAGES)
        checked = await uploads.check(upload(jpeg(800, 600, metadata=50_000)))
        assert (checked.width, checked.height) == (800, 600)
        large = await uploads.check(upload(png(10, 20, padding=10_000)))
        assert (large.width, large.height) == (10, 20)

    async def test_text_is_checked_throughout(self) -> None:
        uploads = FastAPIUploads("text/plain", max_size=1_000_000)
        boundary = b"a" * 65_535 + "é".encode()  # a character across two chunks
        assert (await uploads.check(upload(boundary + b"\n"))).content_type == "text/plain"
        unsupported = (415, "Unsupported file type")
        assert await refused(uploads, b"a" * 10_000 + b"\0") == unsupported
        assert await refused(uploads, b"a" * 10_000 + b"\xff") == unsupported
        assert await refused(uploads, b"a" * 10_000 + "é".encode()[:1]) == unsupported

    async def test_office_documents_from_the_file(self) -> None:
        uploads = FastAPIUploads(OFFICE_DOCUMENTS)
        data = office("word", files={"word/media/image.bin": bytes(range(256)) * 100})
        checked = await uploads.check(upload(data, filename="Report.docx"))
        assert (checked.content_type, checked.extension) == (DOCX, ".docx")
        macros = office("word-macros", files={"word/vbaProject.bin": b"\0" * 10_000})
        assert await refused(uploads, macros) == (415, "Unsupported file type")


class TestConfiguration:
    @pytest.mark.parametrize(
        ("types", "message"),
        [
            ({"image/jpg"}, "use image/jpeg"),
            ({"text/csv"}, "CSV files are text"),
            ({"image/heic", "image/png"}, "the types that can be told apart are"),
            ((), "at least one type"),
        ],
    )
    def test_types(self, types: set[str], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            FastAPIUploads(types)

    def test_limits(self) -> None:
        with pytest.raises(ValueError, match="max_size"):
            FastAPIUploads(IMAGES, max_size=0)
        with pytest.raises(ValueError, match="max_pixels"):
            FastAPIUploads(IMAGES, max_pixels=0)

    def test_a_single_type(self) -> None:
        assert FastAPIUploads("application/pdf").types == {"application/pdf"}
