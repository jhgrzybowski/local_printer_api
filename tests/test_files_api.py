from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app, get_file_storage
from app.services.file_storage import TempFileStorage, sanitize_filename
from tests.helpers import signup_user


@pytest.fixture
def file_client(tmp_path: Path) -> TestClient:
    storage = TempFileStorage(tmp_path, max_upload_mb=1)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_file_storage] = lambda: storage
    with TestClient(app) as client:
        signup_user(client)
        yield client
    app.dependency_overrides.clear()


def make_pdf(page_count: int = 1) -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    return buffer.getvalue()


def make_png() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    image = Image.new("RGB", (4, 4), color="white")
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_upload_rejects_unsupported_mime(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("data.bin", b"\x00\x01\x02", "application/octet-stream")},
    )

    assert response.status_code == 415


def test_upload_rejects_empty_file(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is empty"


def test_pdf_page_count_works(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("two-pages.pdf", make_pdf(2), "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["detected_mime"] == "application/pdf"
    assert body["page_count"] == 2
    assert body["preview_available"] is True


def test_corrupt_pdf_returns_400(file_client: TestClient) -> None:
    response = file_client.post(
        "/files",
        files={"file": ("broken.pdf", b"%PDF-1.7\nnot a valid pdf", "application/pdf")},
    )

    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_preview_endpoint_returns_expected_metadata(file_client: TestClient) -> None:
    upload_response = file_client.post(
        "/files",
        files={"file": ("image.png", make_png(), "image/png")},
    )
    assert upload_response.status_code == 200
    file_id = upload_response.json()["file_id"]

    response = file_client.get(f"/files/{file_id}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["file_id"] == file_id
    assert body["page_count"] == 1
    assert body["pages"][0]["page"] == 1
    assert body["pages"][0]["url"] == f"/files/{file_id}/preview/1"


def test_unknown_file_id_returns_404(file_client: TestClient) -> None:
    response = file_client.get("/files/not-a-real-file/preview")

    assert response.status_code == 404


def test_invalid_preview_page_returns_400(file_client: TestClient) -> None:
    upload_response = file_client.post(
        "/files",
        files={"file": ("image.png", make_png(), "image/png")},
    )
    assert upload_response.status_code == 200
    file_id = upload_response.json()["file_id"]

    response = file_client.get(f"/files/{file_id}/preview/not-a-page")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid page number"


def test_storage_sanitizes_filenames() -> None:
    assert sanitize_filename("../../bad name.pdf") == "bad_name.pdf"
    assert sanitize_filename(r"..\..\nested\file.jpg") == "file.jpg"
