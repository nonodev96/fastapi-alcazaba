"""The uploads example: files judged by their bytes, stored under the app's names, served back
with their real type."""

import io
import struct
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.uploads.main import app

MIB = 1024 * 1024
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAIN_PARTS = {
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "word-macros": "application/vnd.ms-word.document.macroEnabled.main+xml",
}
HTML = b"<!doctype html><html><script>alert(document.cookie)</script></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


# region Sample files


def png(width: int = 64, height: int = 48, padding: int = 0) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = struct.pack(">I", 13) + b"IHDR" + header + b"\0\0\0\0"
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"\0" * padding


def jpeg(width: int = 80, height: int = 60) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\1\x22\0\2\x11\1\3\x11\1"
    scan = b"\xff\xda" + struct.pack(">H", 12) + b"\0" * 10 + b"\xff\xd9"
    return b"\xff\xd8" + sof + scan


def pdf() -> bytes:
    return b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def office(*main: str) -> bytes:
    """A ZIP archive whose [Content_Types].xml declares these main parts, as Office reads it."""
    overrides = "".join(f'<Override PartName="/x" ContentType="{MAIN_PARTS[m]}"/>' for m in main)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f'<?xml version="1.0"?><Types>{overrides}</Types>')
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


# endregion


@pytest.fixture(name="directory")
def fixture_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(name="client")
# pylint: disable-next=unused-argument  # requested for its effect
def fixture_client(directory: Path) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def stored(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def avatar(client: TestClient, data: bytes, name: str = "me.png", kind: str = "image/png") -> Any:
    return client.post("/avatars", files={"file": (name, data, kind)})


def upload_documents(client: TestClient, *files: tuple[str, bytes, str]) -> Any:
    return client.post("/documents", files=[("files", file) for file in files])


class TestAvatars:
    def test_an_image_is_stored_under_a_name_of_the_apps_own(
        self,
        client: TestClient,
        directory: Path,
    ) -> None:
        response = avatar(client, png(64, 48), name="../../home/me.PNG")
        assert response.status_code == 201
        body = response.json()
        assert {key: value for key, value in body.items() if key != "id"} == {
            "name": "me.PNG",
            "content_type": "image/png",
            "size": len(png()),
            "width": 64,
            "height": 48,
        }
        assert stored(directory) == [f"{body['id']}.png"]

    def test_the_type_comes_from_the_bytes_not_the_name(
        self,
        client: TestClient,
        directory: Path,
    ) -> None:
        body = avatar(client, jpeg(), name="cat.png", kind="image/png").json()
        assert (body["content_type"], body["width"], body["height"]) == ("image/jpeg", 80, 60)
        assert stored(directory) == [f"{body['id']}.jpg"]

    @pytest.mark.parametrize(
        ("data", "kind"),
        [
            pytest.param(HTML, "image/png", id="html disguised as png"),
            pytest.param(SVG, "image/svg+xml", id="svg, which can carry scripts"),
            pytest.param(pdf(), "image/png", id="a document"),
        ],
    )
    def test_what_isnt_an_image_is_refused(
        self,
        client: TestClient,
        directory: Path,
        data: bytes,
        kind: str,
    ) -> None:
        response = avatar(client, data, name="cat.png", kind=kind)
        assert (response.status_code, response.json()) == (415, {"detail": "Unsupported file type"})
        assert stored(directory) == []

    def test_an_image_whose_size_cant_be_read_is_refused(self, client: TestClient) -> None:
        response = avatar(client, b"\x89PNG\r\n\x1a\n" + b"\0" * 40)
        assert (response.status_code, response.json()) == (415, {"detail": "Unreadable image"})

    def test_too_many_pixels(self, client: TestClient, directory: Path) -> None:
        # A few bytes that would decode to 1.6 GB of pixels: a decompression bomb.
        response = avatar(client, png(20_000, 20_000))
        assert (response.status_code, response.json()) == (413, {"detail": "Image too large"})
        assert stored(directory) == []

    def test_a_file_over_2_mib(self, client: TestClient) -> None:
        response = avatar(client, png(padding=2 * MIB))
        assert (response.status_code, response.json()) == (413, {"detail": "File too large"})

    def test_a_body_over_the_routes_limit_is_refused_before_it_is_parsed(
        self,
        client: TestClient,
    ) -> None:
        response = avatar(client, png(padding=3 * MIB))
        assert response.status_code == 413
        assert response.json() == {"detail": "Request body too large"}
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_an_empty_file(self, client: TestClient) -> None:
        response = avatar(client, b"")
        assert (response.status_code, response.json()) == (422, {"detail": "Empty file"})

    def test_other_routes_take_no_uploads(self, client: TestClient) -> None:
        response = client.post("/files", files={"file": ("me.png", png(padding=100_000))})
        assert response.status_code == 413


class TestDocuments:
    def test_several_documents(self, client: TestClient, directory: Path) -> None:
        response = upload_documents(
            client,
            ("invoice.pdf", pdf(), PDF),
            ("minutes.docx", office("word"), "application/octet-stream"),
            ("budget.xlsx", office("excel"), XLSX),
        )
        assert response.status_code == 201
        body = response.json()
        assert [(d["name"], d["content_type"]) for d in body] == [
            ("invoice.pdf", PDF),
            ("minutes.docx", DOCX),
            ("budget.xlsx", XLSX),
        ]
        assert stored(directory) == sorted(
            f"{d['id']}{extension}"
            for d, extension in zip(body, [".pdf", ".docx", ".xlsx"], strict=True)
        )

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            pytest.param("report.pdf", HTML, id="html disguised as pdf"),
            pytest.param("macros.docx", office("word-macros"), id="macro-enabled word"),
            pytest.param("archive.docx", office(), id="a zip that isn't a document"),
            pytest.param("photo.pdf", png(), id="an image"),
        ],
    )
    def test_one_bad_file_stores_none(
        self,
        client: TestClient,
        directory: Path,
        name: str,
        data: bytes,
    ) -> None:
        response = upload_documents(client, ("invoice.pdf", pdf(), PDF), (name, data, PDF))
        assert (response.status_code, response.json()) == (415, {"detail": "Unsupported file type"})
        assert stored(directory) == []

    def test_at_most_five_at_a_time(self, client: TestClient, directory: Path) -> None:
        response = upload_documents(client, *[(f"{n}.pdf", pdf(), PDF) for n in range(6)])
        assert response.status_code == 422
        assert stored(directory) == []


class TestDownloads:
    def test_served_as_the_type_found_in_the_bytes(self, client: TestClient) -> None:
        # A Word document sent as a PDF: served as what it is, with the extension that says so.
        document = office("word")
        [uploaded] = upload_documents(client, ("minutes.pdf", document, PDF)).json()
        response = client.get(f"/files/{uploaded['id']}")
        assert response.status_code == 200
        assert response.content == document
        assert response.headers["content-type"] == DOCX
        assert response.headers["content-disposition"] == 'attachment; filename="minutes.docx"'
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_images_are_shown_inline(self, client: TestClient) -> None:
        uploaded = avatar(client, jpeg(), name="cat.png").json()
        response = client.get(f"/files/{uploaded['id']}")
        assert response.content == jpeg()
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["content-disposition"] == 'inline; filename="cat.jpg"'

    @pytest.mark.parametrize(
        ("name", "download"),
        [
            # The right-to-left override shows "invoice\u202efdp.exe" as "invoiceexe.pdf".
            pytest.param("invoice\u202efdp.exe", "invoicefdp.pdf", id="right-to-left override"),
            pytest.param("..\\..\\report:final?.pdf", "report_final_.pdf", id="path and colons"),
            pytest.param("CON.pdf", "_CON.pdf", id="a name windows reserves"),
            pytest.param(".pdf", "pdf.pdf", id="hidden file"),
        ],
    )
    def test_names_are_made_safe(self, client: TestClient, name: str, download: str) -> None:
        [uploaded] = upload_documents(client, (name, pdf(), PDF)).json()
        disposition = client.get(f"/files/{uploaded['id']}").headers["content-disposition"]
        assert disposition == f'attachment; filename="{download}"'

    def test_unknown_files(self, client: TestClient) -> None:
        assert client.get("/files/00000000-0000-4000-8000-000000000000").status_code == 404
        assert client.get("/files/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)
